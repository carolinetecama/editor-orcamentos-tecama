import io
import re
import base64
import fitz  # PyMuPDF
import streamlit as st

st.set_page_config(page_title="Editor de Orçamentos TECAMA", page_icon="📄", layout="wide")

st.title("📄 Editor de Orçamentos TECAMA")
st.caption("Edite os dados do cabeçalho e os valores sem converter o PDF para Word.")

# ----------------------------
# Helpers
# ----------------------------
def br_money_to_float(value: str) -> float:
    if value is None:
        return 0.0
    s = str(value).strip().replace("R$", "").replace(" ", "")
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0

def float_to_br_money(v: float) -> str:
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")

def br_date_to_iso(value: str) -> str:
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", value.strip())
    return value if not m else value

def extract_text(page):
    return page.get_text("text")

def first(pattern, text, default=""):
    m = re.search(pattern, text, flags=re.I | re.S)
    return m.group(1).strip() if m else default

def parse_pdf(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    words = page.get_text("words")

    def text_in_rect(rect):
        x0, y0, x1, y1 = rect
        selected = []
        for w in words:
            wx0, wy0, wx1, wy1, txt = w[:5]
            if wx1 >= x0 and wx0 <= x1 and wy1 >= y0 and wy0 <= y1:
                selected.append((wy0, wx0, txt))
        selected.sort()
        return " ".join(t for _, _, t in selected)

    # Header
    top = text_in_rect((0, 0, 595, 35))
    header = text_in_rect((150, 105, 380, 132))
    client = text_in_rect((10, 145, 590, 185))
    client2 = text_in_rect((10, 181, 590, 225))
    delivery = text_in_rect((10, 232, 590, 275))
    partner = text_in_rect((10, 275, 590, 298))

    data = {}

    m = re.search(r"(\d{2}/\d{2}/\d{4})", top)
    data["data"] = m.group(1) if m else ""
    m = re.search(r"Orçamento\s+([A-Z0-9\-]+)", top, re.I)
    data["orcamento"] = m.group(1) if m else ""

    # The label "CONSULTOR DE VENDAS:" remains untouched; only the name is editable.
    m = re.search(r"VENDAS:\s*(.+)", header, re.I)
    data["consultor"] = m.group(1).strip() if m else ""

    client_lines = [x.strip() for x in client.splitlines() if x.strip()]
    data["cliente"] = ""
    for line in client_lines:
        if not line.upper().startswith("CLIENTE:") and not re.fullmatch(r"\(\d{2}\)\s*\d{4,5}-\d{4}", line):
            if len(line) > 3:
                data["cliente"] = line
                break
    m = re.search(r"(\(\d{2}\)\s*\d{4,5}-\d{4})", client)
    data["telefone_cliente"] = m.group(1) if m else ""

    m = re.search(r"CNPJ:\s*([\d./-]+)", client2, re.I)
    data["cnpj"] = m.group(1) if m else ""
    # Address is the line after CNPJ in this template.
    lines2 = [x.strip() for x in client2.splitlines() if x.strip()]
    data["endereco_cliente"] = lines2[-1] if lines2 else ""

    m = re.search(r"Validade:\s*(\d{2}/\d{2}/\d{4})", delivery, re.I)
    data["validade"] = m.group(1) if m else ""
    m = re.search(r"Previsão de entrega:\s*(\d{2}/\d{2}/\d{4})", delivery, re.I)
    data["entrega"] = m.group(1) if m else ""
    m = re.search(r"Endereço de entrega:\s*(.+)", delivery, re.I)
    data["endereco_entrega"] = m.group(1).strip() if m else ""

    # Keep the partner field as the partner line, without trying to edit its email/phone.
    partner_clean = re.sub(r"^Parceiros?\s*", "", partner, flags=re.I).strip()
    data["parceiro"] = partner_clean

    # Detect every product price row from the original PDF.
    # A product row has TWO R$ tokens at approximately the same Y:
    # one in "Valor unitário" and one in "Total".
    price_rows = []
    rs_words = [w for w in words if w[4] == "R$" and 400 < w[0] < 590]
    for i, rs1 in enumerate(rs_words):
        x0, y0, x1, y1, txt = rs1[:5]
        if not (420 <= x0 <= 450):
            continue
        candidates = []
        for rs2 in rs_words:
            if rs2 is rs1:
                continue
            x20, y20, x21, y21, _ = rs2[:5]
            if 515 <= x20 <= 540 and abs(y20 - y0) <= 3:
                candidates.append(rs2)
        if not candidates:
            continue
        rs2 = min(candidates, key=lambda w: abs(w[1] - y0))

        # Find the numeric word immediately following each R$.
        nums = []
        for rs in (rs1, rs2):
            rx1, ry0 = rs[2], rs[1]
            following = [
                w for w in words
                if w[0] >= rx1 - 1 and w[1] >= ry0 - 1 and w[1] <= ry0 + 3
                and re.fullmatch(r"[\d.]+,\d{2}", w[4])
            ]
            if following:
                nums.append(min(following, key=lambda w: w[0]))
            else:
                nums.append(None)

        if len(nums) != 2 or any(n is None for n in nums):
            continue

        # Quantity from the left side of the same row.
        qty = 1
        qty_candidates = [
            w for w in words
            if w[0] < 70 and abs(w[1] - y0) <= 3 and re.fullmatch(r"\d+", w[4])
        ]
        if qty_candidates:
            try:
                qty = int(qty_candidates[0][4])
            except ValueError:
                qty = 1

        # Description from the item column around this Y, limited to before the configuration text.
        desc_words = [
            w for w in words
            if 140 <= w[0] <= 410 and abs(w[1] - y0) <= 18
        ]
        desc_words.sort(key=lambda w: (w[1], w[0]))
        desc = " ".join(w[4] for w in desc_words)
        desc = re.sub(r"\s+", " ", desc).strip()

        price_rows.append({
            "y": y0,
            "qty": qty,
            "description": desc,
            "unit": nums[0][4],
            "total": nums[1][4],
            "unit_rect": (nums[0][0] - 1, nums[0][1] - 1, nums[0][2] + 1, nums[0][3] + 1),
            "total_rect": (nums[1][0] - 1, nums[1][1] - 1, nums[1][2] + 1, nums[1][3] + 1),
        })

    price_rows.sort(key=lambda x: x["y"])
    # Deduplicate if the PDF extraction contains repeated tokens.
    unique = []
    seen_y = set()
    for row in price_rows:
        key = round(row["y"], 1)
        if key not in seen_y:
            seen_y.add(key)
            unique.append(row)
    data["items"] = unique

    finance = text_in_rect((420, 490, 590, 570))
    payment = text_in_rect((10, 565, 350, 595))
    payment_row = text_in_rect((10, 600, 350, 630))

    m = re.search(r"Frete\s*\+?\s*R\$\s*([\d.]+,\d{2})", finance, re.I)
    data["frete"] = m.group(1) if m else ""
    m = re.search(r"Descontos?\s*-\s*R\$\s*([\d.]+,\d{2})\s*\(([\d,]+)%\)", finance, re.I)
    data["desconto"] = m.group(1) if m else ""
    data["desconto_pct"] = m.group(2) if m else ""

    m = re.search(r"Condição:\s*(.+)", payment, re.I)
    data["condicao"] = m.group(1).strip() if m else ""
    m = re.search(r"(\d{2}/\d{2}/\d{4})", payment_row)
    data["vencimento"] = m.group(1) if m else ""
    m = re.search(r"R\$\s*[\d.]+,\d{2}\s+(\S+)", payment_row)
    data["forma"] = m.group(1) if m else ""

    doc.close()
    return data


def redact_and_write(page, rect, text, font="helv", size=8.5, align=0):
    r = fitz.Rect(rect)
    page.add_redact_annot(r, fill=(1, 1, 1))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
    page.insert_textbox(
        r, str(text or ""), fontname=font, fontsize=size,
        color=(0, 0, 0), align=align, lineheight=1.0, overlay=True
    )

def generate_pdf(pdf_bytes, d):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]

    # Header: replace only the editable fields, not the surrounding labels.
    redact_and_write(page, (276, 110, 375, 129), d["consultor"], "hebo", 8.6)

    # Product prices: use the exact numeric rectangles discovered in the original PDF.
    # This works with 1, 2, 3, ... items instead of hard-coding two rows.
    total = 0.0
    for item in d["items"]:
        unit = br_money_to_float(item["unit"])
        qty = int(item.get("qty", 1) or 1)
        item_total = unit * qty
        total += item_total

        redact_and_write(page, item["unit_rect"], f"{unit:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), "helv", 8.0, align=2)
        redact_and_write(page, item["total_rect"], f"{item_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), "helv", 8.0, align=2)

    frete = br_money_to_float(d["frete"])
    pct = br_money_to_float(d["desconto_pct"])
    desconto_manual = br_money_to_float(d["desconto"])
    desconto = total * pct / 100 if d["desconto_pct"].strip() else desconto_manual
    liquido = total + frete - desconto

    # Summary values
    redact_and_write(page, (542, 496, 579, 512), f"{total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), "hebo", 8.0, align=2)
    redact_and_write(page, (549, 514, 579, 530), f"{frete:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), "helv", 8.0, align=2)
    desc_text = f"{desconto:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    redact_and_write(page, (515, 532, 552, 548), desc_text, "helv", 8.0, align=2)
    if d["desconto_pct"].strip():
        redact_and_write(page, (553, 532, 579, 548), f"({d['desconto_pct']}%)", "helv", 8.0, align=2)
    redact_and_write(page, (542, 551, 579, 568), f"{liquido:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), "hebo", 8.5, align=2)

    # Payment values
    redact_and_write(page, (68, 567, 135, 581), d["condicao"], "hebo", 7.5)
    redact_and_write(page, (100, 579, 155, 594), f"1x {liquido:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), "hebo", 7.5)
    redact_and_write(page, (73, 606, 125, 624), d["vencimento"], "helv", 7.6)
    redact_and_write(page, (183, 606, 239, 624), f"{liquido:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), "helv", 7.6, align=2)
    redact_and_write(page, (241, 606, 275, 624), d["forma"], "helv", 7.6)

    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    return out.getvalue()


# ----------------------------
# UI
# ----------------------------
uploaded = st.file_uploader("Envie o PDF original do Pontta", type=["pdf"])

if uploaded:
    pdf_bytes = uploaded.getvalue()
    try:
        parsed = parse_pdf(pdf_bytes)
    except Exception as e:
        st.error(f"Não consegui ler este PDF: {e}")
        st.stop()

    if not parsed.get("items"):
        st.error("Não encontrei os itens/valores deste modelo de orçamento. Envie o PDF original exportado pelo Pontta.")
        st.stop()

    st.success(f"PDF carregado: {len(parsed['items'])} item(ns) encontrado(s).")

    with st.form("editor"):
        st.subheader("Cabeçalho")

        c1, c2 = st.columns(2)
        with c1:
            data = st.text_input("Data", parsed.get("data", ""))
            orcamento = st.text_input("Nº do orçamento", parsed.get("orcamento", ""))
            consultor = st.text_input("Consultor / vendedor", parsed.get("consultor", ""))
            cliente = st.text_input("Cliente", parsed.get("cliente", ""))
            cnpj = st.text_input("CNPJ", parsed.get("cnpj", ""))
        with c2:
            telefone_cliente = st.text_input("Telefone do cliente", parsed.get("telefone_cliente", ""))
            endereco_cliente = st.text_input("Endereço do cliente", parsed.get("endereco_cliente", ""))
            validade = st.text_input("Validade", parsed.get("validade", ""))
            entrega = st.text_input("Previsão de entrega", parsed.get("entrega", ""))
            endereco_entrega = st.text_input("Endereço de entrega", parsed.get("endereco_entrega", ""))

        st.subheader("Valores dos itens")
        st.caption("O sistema encontrou automaticamente todos os itens. Altere o valor unitário; o total do item e o total do orçamento serão recalculados.")

        edited_items = []
        for i, item in enumerate(parsed["items"], start=1):
            label = item["description"] or f"Item {i}"
            label = label[:90] + ("..." if len(label) > 90 else "")
            cols = st.columns([0.75, 0.25])
            with cols[0]:
                st.markdown(f"**Item {i} — {label}**")
                st.caption(f"Quantidade: {item['qty']} UN")
            with cols[1]:
                unit = st.text_input("Valor unitário", item["unit"], key=f"unit_{i}")
            edited_items.append({
                **item,
                "unit": unit,
            })

        st.subheader("Financeiro")
        c1, c2 = st.columns(2)
        with c1:
            frete = st.text_input("Frete", parsed.get("frete", ""))
            desconto_pct = st.text_input("Desconto (%)", parsed.get("desconto_pct", ""))
            desconto = st.text_input("Desconto (R$) — usado se % estiver vazio", parsed.get("desconto", ""))
        with c2:
            condicao = st.text_input("Condição de pagamento", parsed.get("condicao", ""))
            vencimento = st.text_input("Vencimento", parsed.get("vencimento", ""))
            forma = st.text_input("Forma de pagamento", parsed.get("forma", ""))

        submitted = st.form_submit_button("📄 GERAR PDF EDITADO", type="primary", use_container_width=True)

    if submitted:
        d = {
            "data": data, "orcamento": orcamento, "consultor": consultor,
            "cliente": cliente, "cnpj": cnpj, "telefone_cliente": telefone_cliente,
            "endereco_cliente": endereco_cliente, "validade": validade,
            "entrega": entrega, "endereco_entrega": endereco_entrega,
            "items": edited_items, "frete": frete,
            "desconto_pct": desconto_pct, "desconto": desconto,
            "condicao": condicao, "vencimento": vencimento, "forma": forma,
        }
        try:
            result = generate_pdf(pdf_bytes, d)
            filename = f"{orcamento or 'orcamento'} - EDITADO.pdf"
            st.session_state["result_pdf"] = result
            st.session_state["result_name"] = filename
            st.success("PDF gerado.")
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
            f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="900" '
            'style="border:1px solid #ddd;border-radius:8px;"></iframe>',
            unsafe_allow_html=True,
        )
else:
    st.info("Comece enviando um orçamento PDF exportado do Pontta.")

