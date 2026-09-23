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
    """
    Detect each item from the visual row structure of the Pontta PDF.
    We look for two R$ tokens on the same Y line: unit price and total.
    This is more reliable than relying on PDF text blocks, which can split
    an item into several independent blocks.
    """
    items = []
    for page_index, page in enumerate(doc):
        words = page.get_text("words")

        # Candidate price numbers in the two price columns.
        price_numbers = [
            w for w in words
            if is_money_number(w[4]) and 440 <= w[0] <= 580
        ]

        # Group price numbers by line/Y.
        rows = {}
        for w in price_numbers:
            key = round(w[1], 1)
            rows.setdefault(key, []).append(w)

        for y, row in sorted(rows.items()):
            row = sorted(row, key=lambda w: w[0])
            # Need a unit price around x=444 and a total around x=542.
            unit_candidates = [w for w in row if 440 <= w[0] <= 490]
            total_candidates = [w for w in row if 535 <= w[0] <= 580]
            if not unit_candidates or not total_candidates:
                continue

            unit_w = unit_candidates[0]
            total_w = total_candidates[0]

            # Quantity and item name are on the same line.
            same_line = [w for w in words if abs(w[1] - y) <= 1.5]
            qty_words = [
                w for w in same_line
                if 20 <= w[0] < 70 and re.fullmatch(r"\d+", w[4])
            ]
            if not qty_words:
                continue
            qty = int(qty_words[0][4])

            # Item description lives between x~145 and before the unit price.
            desc_words = [
                w for w in same_line
                if 140 <= w[0] < 410
            ]
            if not desc_words:
                continue
            desc = " ".join(w[4] for w in sorted(desc_words, key=lambda w:w[0])).strip()

            items.append({
                "page": page_index,
                "y": y,
                "qty": qty,
                "description": desc,
                "unit": parse_number(unit_w[4]),
                "total": parse_number(total_w[4]),
                "unit_rect": rect_from_word(unit_w, 1),
                "total_rect": rect_from_word(total_w, 1),
            })

    return items


def detect_finance(doc):
    page_index = len(doc) - 1
    page = doc[page_index]
    words = page.get_text("words")

    def value_after_rs_near_label(label):
        labels = [w for w in words if w[4].lower() == label.lower()]
        if not labels:
            return None
        lab = labels[0]
        rs = [w for w in words if w[4] == "R$" and abs(w[1]-lab[1]) <= 3 and w[0] > lab[2]]
        rs.sort(key=lambda x: x[0])
        if not rs:
            return None
        r = rs[0]
        nums = [w for w in words if is_money_number(w[4]) and abs(w[1]-r[1]) <= 3 and w[0] > r[2]]
        nums.sort(key=lambda x: x[0])
        return nums[0] if nums else None

    total_w = value_after_rs_near_label("Total")
    frete_w = value_after_rs_near_label("Frete")
    desc_w = value_after_rs_near_label("Descontos")
    liquid_w = value_after_rs_near_label("líquido")

    # Exact discount percentage token.
    pct_w = None
    desc_labels = [w for w in words if w[4].lower().startswith("descont")]
    if desc_labels:
        dl = desc_labels[0]
        pct_candidates = [
            w for w in words
            if abs(w[1]-dl[1]) <= 4 and re.fullmatch(r"\(\d+(?:,\d+)?%\)", w[4])
        ]
        if pct_candidates:
            pct_w = sorted(pct_candidates, key=lambda w:w[0])[0]

    blocks = page.get_text("blocks")
    condition = ""
    payment = ""
    condition_rect = None
    payment_rect = None

    for bl in blocks:
        txt = bl[4].strip()
        if txt.startswith("Condição:"):
            condition = txt.splitlines()[0].replace("Condição:", "", 1).strip()
            condition_rect = fitz.Rect(bl[0], bl[1], bl[2], bl[3])
        elif txt.startswith("Pagamento:"):
            payment = txt.splitlines()[0].replace("Pagamento:", "", 1).strip()
            payment_rect = fitz.Rect(bl[0], bl[1], bl[2], bl[3])

    # The table has exactly one amount after each R$ on the same row.
    # Detect by the payment dates, which are stable even when values change.
    payment_rows = []
    for date_w in words:
        if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", date_w[4]):
            continue
        y = date_w[1]
        rs_candidates = [
            w for w in words
            if w[4] == "R$" and abs(w[1]-y) <= 1.5 and w[0] > 175 and w[0] < 210
        ]
        if not rs_candidates:
            continue
        rs_w = min(rs_candidates, key=lambda w:w[0])
        amount_candidates = [
            w for w in words
            if is_money_number(w[4]) and abs(w[1]-y) <= 1.5 and w[0] > rs_w[2] and w[0] < 245
        ]
        if not amount_candidates:
            continue
        amount_w = min(amount_candidates, key=lambda w:w[0])
        payment_rows.append({
            "page": page_index,
            "amount": parse_number(amount_w[4]),
            "rect": rect_from_word(amount_w, 1),
            "date": date_w[4],
            "y": y,
        })

    payment_rows.sort(key=lambda r:r["y"])
    # De-duplicate by row Y.
    clean_rows = []
    seen = set()
    for r in payment_rows:
        k = round(r["y"], 1)
        if k not in seen:
            seen.add(k)
            clean_rows.append(r)

    return {
        "page": page_index,
        "total_rect": rect_from_word(total_w, 1) if total_w else None,
        "frete_rect": rect_from_word(frete_w, 1) if frete_w else None,
        "desconto_rect": rect_from_word(desc_w, 1) if desc_w else None,
        "desconto_pct_rect": rect_from_word(pct_w, 1) if pct_w else None,
        "liquido_rect": rect_from_word(liquid_w, 1) if liquid_w else None,
        "desconto_pct": re.sub(r"[()%]", "", pct_w[4]) if pct_w else "",
        "condition": condition,
        "payment": payment,
        "condition_rect": condition_rect,
        "payment_rect": payment_rect,
        "payment_rows": clean_rows,
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

def parse_payment_percentages(condition):
    """Read the payment percentages from Pontta conditions like:
    34% Sinal + 33 a 28 DDF + 33% a 56 DDF
    The middle installment intentionally may omit the % sign.
    """
    parts = [p.strip() for p in str(condition).split("+") if p.strip()]
    vals = []
    for part in parts:
        m = re.match(r"^(\d+(?:[.,]\d+)?)", part)
        if m:
            vals.append(float(m.group(1).replace(",", ".")))
    return vals

def generate_pdf(pdf_bytes, d):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    p0 = doc[0]
    h = d["header"]
    replace_word(p0, h["name_rect"], d["consultor"], "hebo", 8.5)
    replace_word(p0, h["phone_rect"], d["phone"], "helv", 8.0, align=2)
    replace_word(p0, h["email_rect"], d["email"], "helv", 7.7, align=2)

    total = 0.0
    for item in d["items"]:
        page = doc[item["page"]]
        qty = int(item["qty"])
        unit = round(money_float(item["unit"]), 2)
        item_total = round(qty * unit, 2)
        total = round(total + item_total, 2)
        replace_word(page, item["unit_rect"], money(unit), "helv", 7.8, align=2)
        replace_word(page, item["total_rect"], money(item_total), "helv", 7.8, align=2)

    f = d["finance"]
    frete = round(money_float(d["frete"]), 2)
    if str(d["desconto_pct"]).strip():
        pct = float(str(d["desconto_pct"]).replace(",", "."))
        desconto = round(total * pct / 100, 2)
    else:
        pct = None
        desconto = round(money_float(d["desconto"]), 2)
    liquido = round(total + frete - desconto, 2)
    fp = doc[f["page"]]

    replace_word(fp, f["total_rect"], money(total), "hebo", 8.0, align=2)
    replace_word(fp, f["frete_rect"], money(frete), "helv", 8.0, align=2)
    replace_word(fp, f["desconto_rect"], money(desconto), "helv", 8.0, align=2)
    if f.get("desconto_pct_rect") and pct is not None:
        pct_text = f"({int(pct) if float(pct).is_integer() else str(pct).replace('.', ',')}%)"
        replace_word(fp, f["desconto_pct_rect"], pct_text, "helv", 8.0, align=2)
    replace_word(fp, f["liquido_rect"], money(liquido), "hebo", 8.3, align=2)

    condition = d["condition"]
    percents = [float(x.replace(",", ".")) for x in re.findall(r"(\d+(?:,\d+)?)\s*(?:%|a\s+\d+\s+DDF)", condition, flags=re.I)]
    rows = f.get("payment_rows", [])
    if percents and rows:
        n=min(len(percents),len(rows))
        amounts=[]; remaining=liquido
        for i in range(n):
            if i==n-1: amount=round(remaining,2)
            else:
                amount=round(liquido*percents[i]/100,2)
                remaining=round(remaining-amount,2)
            amounts.append(amount)

        # Replace the entire Pagamento summary line, including ALL old values.
        if f.get("payment_rect"):
            old=fitz.Rect(f["payment_rect"])
            rect=fitz.Rect(old.x0-1,old.y0-1,560,old.y1+1)
            fp.add_redact_annot(rect,fill=(1,1,1))
            fp.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            parts=[]
            for i,amount in enumerate(amounts):
                parts.append(("Entrada" if i==0 else f"{i}x")+f" R$ {money(amount)}")
            fp.insert_textbox(rect,"Pagamento: "+" + ".join(parts),
                              fontname="hebo",fontsize=7.5,color=(0,0,0),
                              align=0,lineheight=1.0,overlay=True)

        # Replace all table amounts from the original coordinates.
        for row,amount in zip(rows[:n],amounts):
            replace_word(fp,row["rect"],money(amount),"helv",7.6,align=2)

    out=io.BytesIO()
    doc.save(out,garbage=4,deflate=True)
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

        # Live calculation preview
        preview_total = sum(int(it["qty"]) * money_float(it["unit"]) for it in edited_items)
        preview_frete = money_float(frete)
        preview_pct = money_float(desconto_pct)
        preview_desconto = round(preview_total * preview_pct / 100, 2) if desconto_pct.strip() else money_float(desconto)
        preview_liquido = round(preview_total + preview_frete - preview_desconto, 2)

        st.markdown("### Conferência antes de gerar")
        pc1, pc2, pc3, pc4 = st.columns(4)
        pc1.metric("Total", f"R$ {money(preview_total)}")
        pc2.metric("Frete", f"R$ {money(preview_frete)}")
        pc3.metric("Desconto", f"R$ {money(preview_desconto)}")
        pc4.metric("Valor líquido", f"R$ {money(preview_liquido)}")


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
