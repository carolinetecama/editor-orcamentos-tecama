import io
import re
import hashlib
import base64

try:
    import pymupdf as fitz
except ImportError:  # versões antigas do PyMuPDF
    import fitz

import streamlit as st

st.set_page_config(page_title="Editor de Orçamentos TECAMA", page_icon="📄", layout="wide")


# ============================================================
# Formatação de valores
# ============================================================
def money_float(value):
    """Converte '1.234,56', '1234,56', '1234.56' ou 'R$ 900' em float."""
    if value is None:
        return 0.0
    s = str(value).strip().replace("R$", "").replace(" ", "")
    if not s:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+\.\d{1,2}", s):
        pass  # 1234.56 -> ponto como decimal
    else:
        s = s.replace(".", "")  # 1.234 -> 1234
    try:
        return float(s)
    except ValueError:
        return 0.0


def money(v):
    s = f"{float(v):,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def pct_text(p):
    p = round(float(p), 2)
    return str(int(p)) if p.is_integer() else f"{p:g}".replace(".", ",")


def parse_number(text):
    m = re.search(r"[\d.]+,\d{2}", str(text))
    return money_float(m.group(0)) if m else 0.0


def is_money_number(s):
    return bool(re.fullmatch(r"[\d.]+,\d{2}", s.strip()))


# ============================================================
# Células editáveis (posição + estilo original)
# ============================================================
def all_spans(page):
    spans = []
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                if s["text"].strip():
                    spans.append(s)
    return spans


def span_for_word(spans, w):
    pt = fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
    for s in spans:
        if fitz.Rect(s["bbox"]).contains(pt):
            return s
    return None


def _color(c):
    return (((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)


def make_cell(page_index, spans, words, style_word, align="left"):
    """Cria uma célula que cobre `words` e copia fonte/tamanho/cor/linha de base
    do texto original (style_word)."""
    x0 = min(w[0] for w in words)
    y0 = min(w[1] for w in words)
    x1 = max(w[2] for w in words)
    y1 = max(w[3] for w in words)
    s = span_for_word(spans, style_word)
    if s:
        bold = bool(s["flags"] & 16) or "bold" in s["font"].lower()
        size = s["size"]
        color = _color(s["color"])
        baseline = s["origin"][1]
    else:  # plano B
        bold, size, color, baseline = False, 9.0, (0, 0, 0), y1 - 3
    return {
        "page": page_index,
        # área apagada = só a altura real dos glifos (não encosta nas linhas vizinhas)
        "rect": fitz.Rect(x0 - 0.5, baseline - size * 0.85, x1 + 0.5, baseline + size * 0.25),
        "x0": x0,
        "x1": x1,
        "baseline": baseline,
        "font": "hebo" if bold else "helv",
        "size": size,
        "color": color,
        "align": align,
    }


def money_cell(page_index, spans, words, num_word, align="right"):
    """Célula do número + o 'R$' que vem logo antes dele (se existir)."""
    group = [num_word]
    for w in words:
        if w[4] == "R$" and abs(w[1] - num_word[1]) < 2.5 and 0 <= num_word[0] - w[2] <= 8:
            group.insert(0, w)
            break
    return make_cell(page_index, spans, group, num_word, align)


def write_cells(doc, ops):
    """ops = [(cell, texto)]. Apaga o original e escreve o novo texto
    com a mesma fonte, tamanho e linha de base."""
    by_page = {}
    for cell, text in ops:
        by_page.setdefault(cell["page"], []).append((cell, text))

    for pi, items in by_page.items():
        page = doc[pi]
        for cell, _ in items:
            page.add_redact_annot(cell["rect"], fill=False)  # sem pintar: preserva o fundo cinza
        try:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            )
        except (TypeError, AttributeError):
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        for cell, text in items:
            width = fitz.get_text_length(text, fontname=cell["font"], fontsize=cell["size"])
            x = cell["x1"] - width if cell["align"] == "right" else cell["x0"]
            page.insert_text(
                (x, cell["baseline"]), text,
                fontname=cell["font"], fontsize=cell["size"], color=cell["color"],
            )


# ============================================================
# Leitura do PDF do Pontta
# ============================================================
def detect_header(page):
    words = page.get_text("words")
    spans = all_spans(page)

    # Nome: palavras na mesma linha depois de "VENDAS:" (antes da coluna de telefone)
    name, name_cell = "", None
    vendas = [w for w in words if w[4].upper().startswith("VENDAS")]
    if vendas:
        v = vendas[0]
        toks = [w for w in words
                if abs(w[1] - v[1]) < 4 and w[0] > v[2] - 1 and w[2] < 470]
        toks.sort(key=lambda w: w[0])
        if toks:
            name = " ".join(w[4] for w in toks)
            name_cell = make_cell(0, spans, toks, toks[0], "left")

    # Telefone e e-mail do consultor (coluna da direita, abaixo do telefone principal)
    right = [w for w in words if w[0] > 470 and 100 <= w[1] <= 135]
    ph = sorted([w for w in right if re.fullmatch(r"\(\d{2}\)", w[4]) or re.fullmatch(r"\d{4,5}-\d{4}", w[4])],
                key=lambda w: w[0])
    phone = " ".join(w[4] for w in ph)
    phone_cell = make_cell(0, spans, ph, ph[0], "right") if ph else None

    em = [w for w in right if "@" in w[4]]
    email = em[0][4] if em else ""
    email_cell = make_cell(0, spans, [em[0]], em[0], "right") if em else None

    return {
        "consultor": name, "phone": phone, "email": email,
        "name_cell": name_cell, "phone_cell": phone_cell, "email_cell": email_cell,
    }


def detect_items(doc):
    """Cada item é ancorado pela linha 'Qtd. + UN'."""
    items = []
    for page_index, page in enumerate(doc):
        words = page.get_text("words")
        spans = all_spans(page)

        for un_w in [w for w in words if w[4].upper() == "UN" and 15 <= w[0] <= 80]:
            q = [w for w in words
                 if abs(w[1] - un_w[1]) <= 2.5 and 15 <= w[2] <= un_w[0] + 1
                 and re.fullmatch(r"\d+(?:[.,]\d+)?", w[4])]
            if not q:
                continue
            q_w = max(q, key=lambda w: w[2])
            qty = float(q_w[4].replace(",", "."))
            qty = int(qty) if qty.is_integer() else qty
            y = un_w[1]

            unit_c = [w for w in words if is_money_number(w[4]) and 400 <= w[0] <= 505 and abs(w[1] - y) <= 6]
            total_c = [w for w in words if is_money_number(w[4]) and 515 <= w[0] <= 590 and abs(w[1] - y) <= 6]
            if not unit_c or not total_c:
                continue
            unit_w = min(unit_c, key=lambda w: abs(w[1] - y))
            total_w = min(total_c, key=lambda w: abs(w[1] - y))

            desc = [w for w in words
                    if 125 <= w[0] < 415 and y - 12 <= w[1] <= y + 16
                    and w[4] != "R$" and not is_money_number(w[4])]
            desc.sort(key=lambda w: (round(w[1]), w[0]))
            description = " ".join(w[4] for w in desc).strip()
            if "Configuração" in description:
                description = description.split("Configuração", 1)[0].strip()
            if not description:
                continue
            if any(k in description.lower() for k in ["condição:", "pagamento:", "descontos", "valor líquido"]):
                continue

            items.append({
                "page": page_index, "y": y, "qty": qty, "description": description,
                "unit": parse_number(unit_w[4]), "total": parse_number(total_w[4]),
                "unit_cell": money_cell(page_index, spans, words, unit_w),
                "total_cell": money_cell(page_index, spans, words, total_w),
            })

    unique, seen = [], set()
    for it in sorted(items, key=lambda x: (x["page"], x["y"])):
        key = (it["page"], round(it["y"], 1), it["description"])
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique


def detect_finance(doc):
    pi = len(doc) - 1
    page = doc[pi]
    words = page.get_text("words")
    spans = all_spans(page)

    def find_row(label):
        """Procura o rótulo que tem um valor em dinheiro na mesma linha
        (ignora o 'Total' do cabeçalho da tabela de itens)."""
        best = None
        for lab in [w for w in words if w[4].lower() == label.lower()]:
            nums = sorted([w for w in words if is_money_number(w[4])
                           and abs(w[1] - lab[1]) <= 2.5 and w[0] > lab[2]],
                          key=lambda w: w[0])
            if nums and (best is None or lab[1] > best[0][1]):
                best = (lab, nums[0])
        return best

    def cell_for(label):
        r = find_row(label)
        if not r:
            return None, 0.0
        return money_cell(pi, spans, words, r[1]), parse_number(r[1][4])

    total_cell, _ = cell_for("Total")
    frete_cell, frete = cell_for("Frete")
    liquido_cell, liquido = cell_for("líquido")

    # Descontos: "- R$ 2.504,32 (28%)" é tratado como uma única célula
    desc_cell, desconto, desc_pct, has_pct = None, 0.0, "", False
    r = find_row("Descontos")
    if r:
        lab, num = r
        pct_w = next((w for w in words if abs(w[1] - lab[1]) <= 2.5 and w[0] > num[2] - 1
                      and re.fullmatch(r"\(\d+(?:,\d+)?%\)", w[4])), None)
        group = [num]
        for w in words:
            if w[4] == "R$" and abs(w[1] - num[1]) < 2.5 and 0 <= num[0] - w[2] <= 8:
                group.insert(0, w)
                break
        if pct_w:
            group.append(pct_w)
            has_pct = True
            desc_pct = re.sub(r"[()%]", "", pct_w[4])
        desc_cell = make_cell(pi, spans, group, num, "right")
        desconto = parse_number(num[4])

    # Condição e linha "Pagamento: 1x R$ ..."
    condition = ""
    for b in page.get_text("blocks"):
        for line in b[4].splitlines():
            if line.strip().startswith("Condição:"):
                condition = line.split("Condição:", 1)[1].strip()
                break
        if condition:
            break

    summary_cells = []
    lab = next((w for w in words if w[4].startswith("Pagamento:")), None)
    if lab:
        for w in sorted(words, key=lambda w: w[0]):
            if is_money_number(w[4]) and abs(w[1] - lab[1]) <= 2.5 and w[0] > lab[2]:
                summary_cells.append(money_cell(pi, spans, words, w, "left"))

    # Tabela de parcelas
    rows, seen = [], set()
    for d in words:
        if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", d[4]):
            continue
        amounts = [w for w in words if is_money_number(w[4]) and abs(w[1] - d[1]) <= 2.5 and 150 < w[0] < 400]
        if not amounts:
            continue
        aw = min(amounts, key=lambda w: w[0])
        k = round(d[1], 1)
        if k in seen:
            continue
        seen.add(k)
        rows.append({"y": d[1], "amount": parse_number(aw[4]), "cell": money_cell(pi, spans, words, aw)})
    rows.sort(key=lambda r: r["y"])

    return {
        "page": pi,
        "total_cell": total_cell, "frete_cell": frete_cell,
        "desc_cell": desc_cell, "liquido_cell": liquido_cell,
        "desconto_pct": desc_pct, "has_pct": has_pct,
        "condition": condition,
        "summary_cells": summary_cells, "payment_rows": rows,
        "frete": frete, "desconto": desconto, "liquido": liquido,
    }


def parse_pdf(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return {
            "header": detect_header(doc[0]),
            "items": detect_items(doc),
            "finance": detect_finance(doc),
            "doc_pages": len(doc),
        }
    finally:
        doc.close()


# ============================================================
# Cálculo (usado tanto na conferência quanto no PDF)
# ============================================================
def calc_finance(items, frete_txt, pct_txt, desc_txt, orig_desc_txt):
    """Retorna dict com total, frete, desconto, pct e líquido.
    Se o usuário mudou o campo Desconto (R$), ele vale; senão vale o %."""
    total = round(sum(float(it["qty"]) * money_float(it["unit"]) for it in items), 2)
    frete = round(money_float(frete_txt), 2)
    desc_changed = str(desc_txt).strip() != str(orig_desc_txt).strip()

    if desc_changed:
        desconto = round(money_float(desc_txt), 2)
        pct = (desconto / total * 100) if total else 0.0
    elif str(pct_txt).strip():
        pct = money_float(pct_txt)
        desconto = round(total * pct / 100, 2)
    else:
        desconto = round(money_float(desc_txt), 2)
        pct = (desconto / total * 100) if total else 0.0

    return {
        "total": total, "frete": frete, "desconto": desconto, "pct": pct,
        "liquido": round(total + frete - desconto, 2),
    }


def parse_payment_percentages(condition):
    """'34% Sinal + 33 a 28 DDF + 33% a 56 DDF' -> [34, 33, 33]"""
    vals = []
    for part in str(condition).split("+"):
        m = re.match(r"^\s*(\d+(?:[.,]\d+)?)", part)
        if m:
            vals.append(float(m.group(1).replace(",", ".")))
    return vals


def split_payments(liquido, percents, n_rows):
    if not n_rows:
        return []
    if not percents:
        return [liquido] if n_rows == 1 else []
    n = min(len(percents), n_rows)
    amounts, remaining = [], liquido
    for i in range(n):
        if i == n - 1:
            a = round(remaining, 2)
        else:
            a = round(liquido * percents[i] / 100, 2)
            remaining = round(remaining - a, 2)
        amounts.append(a)
    return amounts


# ============================================================
# Geração do PDF editado
# ============================================================
def generate_pdf(pdf_bytes, parsed, d):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    warnings = []
    ops = []

    # 1) Cabeçalho
    h = parsed["header"]
    if h["name_cell"]:
        ops.append((h["name_cell"], d["consultor"]))
    if h["phone_cell"]:
        ops.append((h["phone_cell"], d["phone"]))
    if h["email_cell"]:
        ops.append((h["email_cell"], d["email"]))

    # 2) Itens
    for it in d["items"]:
        unit = round(money_float(it["unit"]), 2)
        item_total = round(float(it["qty"]) * unit, 2)
        ops.append((it["unit_cell"], "R$ " + money(unit)))
        ops.append((it["total_cell"], "R$ " + money(item_total)))

    # 3) Financeiro
    f = parsed["finance"]
    calc = calc_finance(d["items"], d["frete"], d["desconto_pct"], d["desconto"], d["orig_desconto"])
    if f["total_cell"]:
        ops.append((f["total_cell"], "R$ " + money(calc["total"])))
    else:
        warnings.append("Não encontrei o campo Total no PDF.")
    if f["frete_cell"]:
        ops.append((f["frete_cell"], "R$ " + money(calc["frete"])))
    if f["desc_cell"]:
        txt = "R$ " + money(calc["desconto"])
        if f["has_pct"]:
            txt += f" ({pct_text(calc['pct'])}%)"
        ops.append((f["desc_cell"], txt))
    if f["liquido_cell"]:
        ops.append((f["liquido_cell"], "R$ " + money(calc["liquido"])))
    else:
        warnings.append("Não encontrei o campo Valor líquido no PDF.")

    # 4) Pagamento: atualiza apenas os números existentes
    percents = parse_payment_percentages(d["condition"])
    rows = f["payment_rows"]
    amounts = split_payments(calc["liquido"], percents, len(rows))
    if percents and len(percents) != len(rows):
        warnings.append(
            f"A condição tem {len(percents)} parcela(s), mas o PDF tem {len(rows)} linha(s) de parcela. "
            "Só as linhas existentes foram atualizadas."
        )
    if percents and abs(sum(percents) - 100) > 0.01:
        warnings.append(f"As porcentagens da condição somam {pct_text(sum(percents))}%, não 100%.")

    for row, a in zip(rows, amounts):
        ops.append((row["cell"], "R$ " + money(a)))
    for cell, a in zip(f["summary_cells"], amounts):
        ops.append((cell, "R$ " + money(a)))

    write_cells(doc, ops)

    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    return out.getvalue(), warnings


# ============================================================
# Interface
# ============================================================
st.title("📄 Editor de Orçamentos TECAMA")
st.caption("Altera consultor, contato e valores mantendo o PDF original como base.")

uploaded = st.file_uploader("Envie o orçamento original exportado do Pontta", type=["pdf"])

if uploaded:
    pdf_bytes = uploaded.getvalue()
    sig = hashlib.sha256(pdf_bytes).hexdigest()[:12]  # chaves únicas por PDF

    if st.session_state.get("_upload_signature") != sig:
        st.session_state["_upload_signature"] = sig
        st.session_state.pop("result", None)

    try:
        parsed = parse_pdf(pdf_bytes)
    except Exception as e:
        st.error(f"Erro ao analisar o PDF: {e}")
        st.stop()

    st.success(f"Encontrados {len(parsed['items'])} item(ns) em {parsed['doc_pages']} página(s).")
    if not parsed["items"]:
        st.error("Não consegui localizar os itens deste PDF. Nenhum PDF será gerado para evitar alterar o orçamento incorretamente.")
        st.stop()

    hd, fin = parsed["header"], parsed["finance"]

    st.subheader("Consultor de vendas")
    c1, c2, c3 = st.columns(3)
    consultor = c1.text_input("Nome", hd["consultor"], key=f"nome_{sig}")
    phone = c2.text_input("Telefone", hd["phone"], key=f"tel_{sig}")
    email = c3.text_input("E-mail", hd["email"], key=f"mail_{sig}")

    st.subheader("Valores dos itens")
    st.caption("Altere somente o valor unitário. O total de cada item e o total do orçamento são recalculados com a quantidade original.")
    edited_items = []
    for i, item in enumerate(parsed["items"], 1):
        a, b, c = st.columns([0.55, 0.20, 0.25])
        a.write(f"**{i}. {item['description']}**")
        b.write(f"{item['qty']} UN")
        unit = c.text_input("Valor unitário", money(item["unit"]), key=f"unit_{sig}_{item['page']}_{i}")
        edited_items.append({**item, "unit": unit})

    st.subheader("Financeiro")
    orig_desc_txt = money(fin["desconto"])
    c1, c2, c3 = st.columns(3)
    frete = c1.text_input("Frete", money(fin["frete"]), key=f"frete_{sig}")
    desconto_pct = c2.text_input("Desconto (%)", fin["desconto_pct"], key=f"dpct_{sig}")
    desconto = c3.text_input("Desconto (R$)", orig_desc_txt, key=f"dval_{sig}")
    st.caption("Para dar desconto em %, altere o campo (%). Para dar desconto em reais, altere o campo (R$); nesse caso o (%) é recalculado.")

    st.subheader("Pagamento")
    condition = st.text_input("Condição de pagamento", fin["condition"], key=f"cond_{sig}")

    calc = calc_finance(edited_items, frete, desconto_pct, desconto, orig_desc_txt)
    percents = parse_payment_percentages(condition)
    amounts = split_payments(calc["liquido"], percents, len(fin["payment_rows"]))

    st.markdown("### Conferência antes de gerar")
    pc1, pc2, pc3, pc4 = st.columns(4)
    pc1.metric("Total", f"R$ {money(calc['total'])}")
    pc2.metric("Frete", f"R$ {money(calc['frete'])}")
    pc3.metric("Desconto", f"R$ {money(calc['desconto'])} ({pct_text(calc['pct'])}%)")
    pc4.metric("Valor líquido", f"R$ {money(calc['liquido'])}")
    if amounts:
        st.caption("Parcelas: " + " + ".join(f"R$ {money(a)}" for a in amounts))

    state = repr((consultor, phone, email, [it["unit"] for it in edited_items],
                  frete, desconto_pct, desconto, condition))

    if st.button("📄 GERAR PDF EDITADO", type="primary", use_container_width=True):
        d = {
            "consultor": consultor, "phone": phone, "email": email,
            "items": edited_items, "frete": frete,
            "desconto_pct": desconto_pct, "desconto": desconto,
            "orig_desconto": orig_desc_txt, "condition": condition,
        }
        try:
            data, warns = generate_pdf(pdf_bytes, parsed, d)
            st.session_state["result"] = {"pdf": data, "state": state, "warnings": warns}
        except Exception as e:
            st.exception(e)

    res = st.session_state.get("result")
    if res:
        if res["state"] != state:
            st.warning("Você alterou valores depois de gerar. Clique em GERAR PDF EDITADO novamente.")
        else:
            st.success("PDF gerado com os valores recalculados.")
            for w in res["warnings"]:
                st.warning(w)
            st.download_button(
                "⬇️ Baixar PDF editado", data=res["pdf"],
                file_name="Orçamento - EDITADO.pdf", mime="application/pdf",
                use_container_width=True,
            )
            b64 = base64.b64encode(res["pdf"]).decode()
            st.markdown(
                f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="850" '
                'style="border:1px solid #ddd;border-radius:8px;"></iframe>',
                unsafe_allow_html=True,
            )
else:
    st.info("Envie o PDF original do Pontta para começar.")
