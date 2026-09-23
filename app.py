import io
import re
import base64
import fitz
import streamlit as st

st.set_page_config(page_title="Editor de Orçamentos TECAMA", page_icon="📄", layout="wide")

# ============================================================
# Formatação
# ============================================================
def money_float(value):
    if value is None:
        return 0.0
    s = str(value).strip().replace("R$", "").replace(" ", "")
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0

def money(v):
    s = f"{float(v):,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")

def money_with_prefix(v):
    return "R$ " + money(v)

def parse_number(text):
    m = re.search(r"[\d.]+,\d{2}", str(text))
    return money_float(m.group(0)) if m else 0.0

def is_money_number(s):
    return bool(re.fullmatch(r"[\d.]+,\d{2}", s.strip()))

# ============================================================
# Localização de textos no PDF
# ============================================================
def words_on_page(page):
    return page.get_text("words")

def rect_from_word(w, pad=0.5):
    return fitz.Rect(w[0]-pad, w[1]-pad, w[2]+pad, w[3]+pad)

def find_words(words, text):
    return [w for w in words if w[4].strip().upper() == text.upper()]

def find_money_after(words, anchor_word, min_x=None, max_y=4):
    ax1 = anchor_word[2]
    ay = anchor_word[1]
    candidates = []
    for w in words:
        if not is_money_number(w[4]):
            continue
        if w[0] < ax1 - 1:
            continue
        if abs(w[1] - ay) > max_y:
            continue
        if min_x is not None and w[0] < min_x:
            continue
        candidates.append(w)
    candidates.sort(key=lambda w: w[0])
    return candidates[0] if candidates else None

def detect_header(page):
    words = words_on_page(page)

    def text_between(y0, y1):
        arr = [w for w in words if y0 <= w[1] <= y1]
        arr.sort(key=lambda w: (w[1], w[0]))
        return " ".join(w[4] for w in arr)

    header_text = text_between(100, 132)

    # Consultor: name appears after "VENDAS:".
    consultant_words = []
    after_vendas = False
    for w in sorted([x for x in words if 100 <= x[1] <= 132], key=lambda w:(w[1],w[0])):
        t = w[4]
        if t.upper().startswith("VENDAS"):
            after_vendas = True
            continue
        if after_vendas and t not in ["(11)", "93061-2286"] and "@" not in t:
            consultant_words.append(w)

    # More robustly use the word "CONSULTOR" block and detect line positions.
    name = ""
    phone = ""
    email = ""
    for w in words:
        t = w[4]
        if t == "CAROLINE" or t == "WILSON":
            pass

    # Phone/email are easy to detect by pattern.
    phone_words = [w for w in words if re.fullmatch(r"\(\d{2}\)", w[4]) or re.fullmatch(r"\d{4,5}-\d{4}", w[4])]
    # Keep only header-area phone.
    phone_words = [w for w in phone_words if 100 <= w[1] <= 132]
    if phone_words:
        # In this PDF phone may be a single block split into 2 words.
        phone = " ".join(w[4] for w in sorted(phone_words, key=lambda w:w[0]))

    email_words = [w for w in words if "@" in w[4] and 100 <= w[1] <= 132]
    email = email_words[0][4] if email_words else ""

    # Find the text block that starts with CONSULTOR DE VENDAS.
    blocks = page.get_text("blocks")
    consultant_block = None
    for b in blocks:
        txt = b[4]
        if "CONSULTOR DE VENDAS:" in txt.upper():
            consultant_block = b
            break

    if consultant_block:
        x0,y0,x1,y1 = consultant_block[:4]
        cw = [w for w in words if w[1] >= y0-1 and w[3] <= y1+1 and w[0] >= x0-1 and w[2] <= x1+1]
        # Remove label/phone/email and keep name tokens.
        name_tokens = []
        for w in sorted(cw, key=lambda w:(w[1],w[0])):
            t = w[4]
            if t.upper() in {"CONSULTOR","DE","VENDAS:"}:
                continue
            if "@" in t or re.fullmatch(r"\(\d{2}\)", t) or re.fullmatch(r"\d{4,5}-\d{4}", t):
                continue
            if t.upper().startswith("VENDAS"):
                continue
            name_tokens.append(w)
        if name_tokens:
            # Use only tokens on the first line after the label; this is the name.
            first_y = min(w[1] for w in name_tokens)
            line_tokens = [w for w in name_tokens if abs(w[1]-first_y) < 3]
            name = " ".join(w[4] for w in sorted(line_tokens, key=lambda w:w[0]))

    # Exact editable rectangles from the detected words.
    name_words = [w for w in words if w[4] == name] if name else []
    if name_words:
        name_rect = rect_from_word(name_words[0], 1)
    else:
        # Template fallback: name area after label.
        name_rect = fitz.Rect(275, 108, 375, 123)

    # Phone and email exact rectangles by their words.
    pw = [w for w in words if w[4] == "(11)" and 100 <= w[1] <= 132]
    phone_rect = None
    if pw:
        p0 = pw[0]
        # include next phone token
        nxt = [w for w in words if 100 <= w[1] <= 132 and w[0] > p0[2] and w[0] < p0[2]+80]
        x1 = max([p0[2]] + [w[2] for w in nxt])
        phone_rect = fitz.Rect(p0[0]-1, p0[1]-1, x1+1, p0[3]+1)
    if phone_rect is None:
        phone_rect = fitz.Rect(495, 109, 578, 122)

    ew = [w for w in words if "@" in w[4] and 100 <= w[1] <= 132]
    email_rect = rect_from_word(ew[0], 1) if ew else fitz.Rect(495, 121, 580, 132)

    return {
        "consultor": name,
        "phone": phone,
        "email": email,
        "name_rect": name_rect,
        "phone_rect": phone_rect,
        "email_rect": email_rect,
    }

def detect_items(doc):
    items = []
    for page_index, page in enumerate(doc):
        blocks = page.get_text("blocks")
        words = words_on_page(page)

        for b in blocks:
            x0,y0,x1,y1,txt = b[:5]
            if not re.search(r"\b\d+\s+UN\b", txt):
                continue
            if "R$" not in txt:
                continue

            # Extract quantity from block.
            qm = re.search(r"(\d+)\s+UN", txt)
            if not qm:
                continue
            qty = int(qm.group(1))

            # Identify money numbers on this item row by y.
            row_words = [w for w in words if abs(w[1]-y0) <= 3 and w[0] >= x0-2 and w[0] <= x1+2]
            money_words = [w for w in row_words if is_money_number(w[4])]
            # Original PDFs have two numeric price words: unit and total.
            if len(money_words) < 2:
                # Some PDFs split "R$" and numeric values but still keep numbers in block.
                money_words = [w for w in words if is_money_number(w[4]) and abs(w[1]-y0) <= 5 and w[0] > 400]
            if len(money_words) < 2:
                continue
            money_words = sorted(money_words, key=lambda w:w[0])
            unit_w, total_w = money_words[-2], money_words[-1]

            # Description: first line of block after quantity and before R$.
            lines = [re.sub(r"\s+", " ", ln).strip() for ln in txt.splitlines() if ln.strip()]
            desc = ""
            for ln in lines:
                if " UN " in f" {ln} ":
                    desc = re.sub(r"^\d+\s+UN\s*", "", ln, flags=re.I).strip()
                    break
            if not desc:
                continue

            items.append({
                "page": page_index,
                "y": y0,
                "qty": qty,
                "description": desc,
                "unit": parse_number(unit_w[4]),
                "total": parse_number(total_w[4]),
                "unit_rect": rect_from_word(unit_w, 1),
                "total_rect": rect_from_word(total_w, 1),
            })

    return items

def detect_finance(doc):
    # Finance is normally on the last page. Find the exact numeric words
    # associated with the labels instead of hard-coded coordinates.
    page_index = len(doc)-1
    page = doc[page_index]
    words = words_on_page(page)

    def label_and_value(label, y_hint=None):
        labels = [w for w in words if w[4].lower().startswith(label.lower())]
        if y_hint is not None:
            labels = sorted(labels, key=lambda w: abs(w[1]-y_hint))
        if not labels:
            return None, None
        lab = labels[0]
        candidates = [w for w in words if is_money_number(w[4]) and abs(w[1]-lab[1]) <= 3 and w[0] > lab[2]]
        candidates.sort(key=lambda w:w[0])
        return lab, (candidates[0] if candidates else None)

    total_label, total_w = label_and_value("Total")
    frete_label, frete_w = label_and_value("Frete")
    desc_label, desc_w = label_and_value("Descontos")
    liquid_label, liquid_w = label_and_value("Valor")

    # "Valor" may return another value; locate the exact label.
    vlabs = [w for w in words if w[4].lower() == "líquido"]
    if vlabs:
        vl = vlabs[0]
        candidates = [w for w in words if is_money_number(w[4]) and abs(w[1]-vl[1]) <= 3 and w[0] > vl[2]]
        candidates.sort(key=lambda w:w[0])
        liquid_w = candidates[0] if candidates else liquid_w

    # Discount percentage is a separate numeric token in parentheses.
    pct = ""
    if desc_label:
        near = [w for w in words if abs(w[1]-desc_label[1]) <= 4 and re.fullmatch(r"\(\d+,\d+%\)|\(\d+%\)", w[4])]
        if near:
            pct = re.sub(r"[()%]", "", near[0][4])

    # Condition / payment text blocks
    blocks = page.get_text("blocks")
    condition = ""
    payment = ""
    condition_rect = None
    payment_rect = None
    for b in blocks:
        txt = b[4].strip()
        if txt.startswith("Condição:"):
            condition = re.sub(r"^Condição:\s*", "", txt.splitlines()[0]).strip()
            condition_rect = fitz.Rect(b[0], b[1], b[2], b[3])
        if txt.startswith("Pagamento:"):
            payment = re.sub(r"^Pagamento:\s*", "", txt.splitlines()[0]).strip()
            payment_rect = fitz.Rect(b[0], b[1], b[2], b[3])

    # Payment table rows. Keep labels/dates/form untouched, replace only amounts.
    payment_rows = []
    for b in blocks:
        txt = b[4].strip()
        if not txt or "Boleto" not in txt:
            continue
        lines = txt.splitlines()
        for line in lines:
            if "Boleto" in line:
                nums = [w for w in words if is_money_number(w[4]) and b[1]-1 <= w[1] <= b[3]+1]
                dates = [w for w in words if re.fullmatch(r"\d{2}/\d{2}/\d{4}", w[4]) and b[1]-1 <= w[1] <= b[3]+1]
                if nums:
                    # Last numeric token in this block is the payment amount.
                    aw = sorted(nums, key=lambda w:w[0])[-1]
                    payment_rows.append({
                        "page": page_index,
                        "amount": parse_number(aw[4]),
                        "rect": rect_from_word(aw, 1),
                        "label": line.split()[0] if line.split() else "",
                    })

    return {
        "page": page_index,
        "total_rect": rect_from_word(total_w, 1) if total_w else None,
        "frete_rect": rect_from_word(frete_w, 1) if frete_w else None,
        "desconto_rect": rect_from_word(desc_w, 1) if desc_w else None,
        "liquido_rect": rect_from_word(liquid_w, 1) if liquid_w else None,
        "desconto_pct": pct,
        "condition": condition,
        "payment": payment,
        "condition_rect": condition_rect,
        "payment_rect": payment_rect,
        "payment_rows": payment_rows,
        "frete": parse_number(frete_w[4]) if frete_w else 0,
        "desconto": parse_number(desc_w[4]) if desc_w else 0,
        "liquido": parse_number(liquid_w[4]) if liquid_w else 0,
    }

def parse_pdf(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    first_page = doc[0]
    header = detect_header(first_page)
    items = detect_items(doc)
    finance = detect_finance(doc)
    data = {
        "header": header,
        "items": items,
        "finance": finance,
        "doc_pages": len(doc),
    }
    doc.close()
    return data

# ============================================================
# Edição mantendo o PDF original
# ============================================================
def replace_word(page, rect, text, font="helv", size=8.0, align=0):
    if rect is None:
        return
    page.add_redact_annot(fitz.Rect(rect), fill=(1,1,1))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
    page.insert_textbox(
        fitz.Rect(rect), str(text),
        fontname=font, fontsize=size, color=(0,0,0),
        align=align, lineheight=1.0, overlay=True
    )

def update_payment_text(page, rect, new_text):
    if rect is None:
        return
    # Replace the entire payment line because the number of installments
    # and their values may change when the liquid amount changes.
    page.add_redact_annot(rect, fill=(1,1,1))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
    page.insert_textbox(rect, new_text, fontname="helv", fontsize=7.5,
                        color=(0,0,0), align=0, lineheight=1.0, overlay=True)

def generate_pdf(pdf_bytes, d):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    # Header: only the actual values are replaced.
    p0 = doc[0]
    h = d["header"]
    replace_word(p0, h["name_rect"], d["consultor"], "hebo", 8.5)
    replace_word(p0, h["phone_rect"], d["phone"], "helv", 8.0, align=2)
    replace_word(p0, h["email_rect"], d["email"], "helv", 7.7, align=2)

    # Items across all pages.
    total = 0.0
    for item in d["items"]:
        page = doc[item["page"]]
        qty = int(item["qty"])
        unit = money_float(item["unit"])
        item_total = qty * unit
        total += item_total

        replace_word(page, item["unit_rect"], money(unit), "helv", 7.8, align=2)
        replace_word(page, item["total_rect"], money(item_total), "helv", 7.8, align=2)

    f = d["finance"]
    frete = money_float(d["frete"])
    pct = money_float(d["desconto_pct"])
    desconto = total * pct / 100 if d["desconto_pct"].strip() else money_float(d["desconto"])
    liquido = total + frete - desconto

    fp = doc[f["page"]]
    replace_word(fp, f["total_rect"], money(total), "hebo", 8.0, align=2)
    replace_word(fp, f["frete_rect"], money(frete), "helv", 8.0, align=2)
    replace_word(fp, f["desconto_rect"], money(desconto), "helv", 8.0, align=2)
    replace_word(fp, f["liquido_rect"], money(liquido), "hebo", 8.3, align=2)

    # Payment schedule: recalculate based on the original condition.
    # If the condition has percentages (e.g. 34% + 33% + 33%), use them.
    condition = d["condition"]
    percents = [float(x.replace(",", ".")) for x in re.findall(r"(\d+(?:,\d+)?)\s*%", condition)]
    if percents and f["payment_rows"]:
        vals = []
        remaining = liquido
        for i, pct_i in enumerate(percents):
            if i == len(percents)-1:
                amount = remaining
            else:
                amount = round(liquido * pct_i / 100, 2)
                remaining -= amount
            vals.append(amount)

        # Replace the payment line with the new values while preserving wording.
        # The original condition is left untouched.
        if f["payment_rect"] and vals:
            # Generate a concise line following the Pontta style.
            labels = ["Entrada"] + [f"{i}x" for i in range(1, len(vals))]
            parts = []
            for i, amount in enumerate(vals):
                if i == 0:
                    parts.append(f"Entrada R$ {money(amount)}")
                else:
                    parts.append(f"{i}x R$ {money(amount)}")
            new_payment = "Pagamento: " + " + ".join(parts)
            update_payment_text(fp, f["payment_rect"], new_payment)

        # Payment table values.
        for row, amount in zip(f["payment_rows"], vals):
            replace_word(fp, row["rect"], money(amount), "helv", 7.6, align=2)

    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    return out.getvalue()

# ============================================================
# Interface
# ============================================================
st.title("📄 Editor de Orçamentos TECAMA")
st.caption("Altera consultor, contato e valores mantendo o PDF original como base.")

uploaded = st.file_uploader("Envie o orçamento original exportado do Pontta", type=["pdf"])

if uploaded:
    pdf_bytes = uploaded.getvalue()
    try:
        parsed = parse_pdf(pdf_bytes)
    except Exception as e:
        st.error(f"Erro ao analisar o PDF: {e}")
        st.stop()

    st.success(f"Encontrados {len(parsed['items'])} item(ns) em {parsed['doc_pages']} página(s).")

    with st.form("editor"):
        st.subheader("Consultor de vendas")
        c1, c2, c3 = st.columns(3)
        with c1:
            consultor = st.text_input("Nome", parsed["header"]["consultor"])
        with c2:
            phone = st.text_input("Telefone", parsed["header"]["phone"])
        with c3:
            email = st.text_input("E-mail", parsed["header"]["email"])

        st.subheader("Valores dos itens")
        st.caption("Altere somente o valor unitário. O total de cada item e o total do orçamento serão recalculados usando a quantidade original.")

        edited_items = []
        for i, item in enumerate(parsed["items"], 1):
            a, b, c = st.columns([0.55, 0.20, 0.25])
            with a:
                st.write(f"**{i}. {item['description']}**")
            with b:
                st.write(f"{item['qty']} UN")
            with c:
                unit = st.text_input("Valor unitário", money(item["unit"]), key=f"unit_{i}")
            edited_items.append({**item, "unit": unit})

        st.subheader("Financeiro")
        c1, c2, c3 = st.columns(3)
        with c1:
            frete = st.text_input("Frete", money(parsed["finance"]["frete"]))
        with c2:
            desconto_pct = st.text_input("Desconto (%)", parsed["finance"]["desconto_pct"])
        with c3:
            desconto = st.text_input("Desconto (R$)", money(parsed["finance"]["desconto"]))

        st.subheader("Pagamento")
        condition = st.text_input("Condição de pagamento", parsed["finance"]["condition"])

        submitted = st.form_submit_button("📄 GERAR PDF EDITADO", type="primary", use_container_width=True)

    if submitted:
        d = {
            "header": parsed["header"],
            "consultor": consultor,
            "phone": phone,
            "email": email,
            "items": edited_items,
            "frete": frete,
            "desconto_pct": desconto_pct,
            "desconto": desconto,
            "condition": condition,
            "finance": parsed["finance"],
        }
        try:
            result = generate_pdf(pdf_bytes, d)
            st.session_state["result_pdf"] = result
            st.session_state["result_name"] = "Orçamento - EDITADO.pdf"
            st.success("PDF gerado com os valores recalculados.")
        except Exception as e:
            st.exception(e)

    if "result_pdf" in st.session_state:
        st.download_button(
            "⬇️ Baixar PDF editado",
            data=st.session_state["result_pdf"],
            file_name=st.session_state["result_name"],
            mime="application/pdf",
            use_container_width=True,
        )
        b64 = base64.b64encode(st.session_state["result_pdf"]).decode()
        st.markdown(
            f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="850" '
            'style="border:1px solid #ddd;border-radius:8px;"></iframe>',
            unsafe_allow_html=True,
        )
else:
    st.info("Envie o PDF original do Pontta para começar.")
