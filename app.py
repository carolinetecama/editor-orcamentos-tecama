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
    blocks = page.get_text("blocks")

    def block_at(y1, y2, x1=0, x2=595):
        candidates = []
        for b in blocks:
            x0, top, x_1, bottom, txt = b[:5]
            if top >= y1 and bottom <= y2 and x_1 >= x1 and x0 <= x2:
                candidates.append((top, txt))
        candidates.sort()
        return "\n".join(t for _, t in candidates).strip()

    top = block_at(0, 35)
    client = block_at(145, 181)
    client2 = block_at(181, 225)
    delivery = block_at(235, 272)
    partner = block_at(270, 296)
    item1 = block_at(345, 375)
    item2 = block_at(438, 460)
    finance = block_at(495, 568)
    payment = block_at(567, 592)
    payment_row = block_at(605, 624)

    data = {}
    m = re.search(r"^(\d{2}/\d{2}/\d{4})", top)
    data["data"] = m.group(1) if m else ""
    m = re.search(r"Orçamento\s+([A-Z0-9\-]+)", top, re.I)
    data["orcamento"] = m.group(1) if m else ""

    client_lines = [x.strip() for x in client.splitlines() if x.strip()]
    data["cliente"] = client_lines[1] if len(client_lines) >= 2 else ""
    m = re.search(r"(\(\d{2}\)\s*\d{4,5}-\d{4})", client)
    data["telefone_cliente"] = m.group(1) if m else ""

    client2_lines = [x.strip() for x in client2.splitlines() if x.strip()]
    m = re.search(r"CNPJ:\s*([\d./-]+)", client2)
    data["cnpj"] = m.group(1) if m else ""
    if client2_lines:
        data["endereco_cliente"] = client2_lines[-1]
    else:
        data["endereco_cliente"] = ""

    m = re.search(r"Validade:\s*(\d{2}/\d{2}/\d{4})", delivery)
    data["validade"] = m.group(1) if m else ""
    m = re.search(r"Previsão de entrega:\s*(\d{2}/\d{2}/\d{4})", delivery)
    data["entrega"] = m.group(1) if m else ""
    m = re.search(r"Endereço de entrega:\s*(.+)", delivery)
    data["endereco_entrega"] = m.group(1).strip() if m else ""

    partner_lines = [x.strip() for x in partner.splitlines() if x.strip()]
    data["parceiro"] = " ".join(partner_lines[1:]) if len(partner_lines) > 1 else ""

    def item_parse(txt):
        lines = [re.sub(r"\s+", " ", x).strip() for x in txt.splitlines() if x.strip()]
        desc = ""
        vals = []
        for line in lines:
            m = re.search(r"R\$\s*([\d.]+,\d{2})", line)
            if m:
                vals.append(m.group(1))
            elif not re.fullmatch(r"\d+\s+UN", line, re.I):
                desc = (desc + " " + line).strip()
        return desc, vals[0] if vals else ""

    data["item1_desc"], data["item1_unit"] = item_parse(item1)
    data["item2_desc"], data["item2_unit"] = item_parse(item2)

    m = re.search(r"Total\s*\nR\$\s*([\d.]+,\d{2})", finance, re.I)
    data["total"] = m.group(1) if m else ""
    m = re.search(r"Frete\s*\n\+\s*R\$\s*([\d.]+,\d{2})", finance, re.I)
    data["frete"] = m.group(1) if m else ""
    m = re.search(r"Descontos\s*\n-\s*R\$\s*([\d.]+,\d{2})\s*\(([\d,]+)%\)", finance, re.I)
    data["desconto"] = m.group(1) if m else ""
    data["desconto_pct"] = m.group(2) if m else ""
    m = re.search(r"Valor líquido\s*\nR\$\s*([\d.]+,\d{2})", finance, re.I)
    data["liquido"] = m.group(1) if m else ""

    m = re.search(r"Condição:\s*(.+)", payment)
    data["condicao"] = m.group(1).strip() if m else ""
    m = re.search(r"(\d{2}/\d{2}/\d{4})", payment_row)
    data["vencimento"] = m.group(1) if m else ""
    m = re.search(r"R\$\s*[\d.]+,\d{2}\s+(\S+)", payment_row)
    data["forma"] = m.group(1) if m else ""

    doc.close()
    return data

def fit_text(page, rect, text, fontname="helv", fontsize=9, color=(0,0,0), align=0, min_size=5.5):
    """Insert text, reducing font size if needed."""
    size = fontsize
    while size >= min_size:
        spare = page.insert_textbox(
            rect, str(text or ""), fontname=fontname, fontsize=size,
            color=color, align=align, lineheight=1.0
        )
        if spare >= -0.5:
            return
        # Remove the text just inserted by reverting page edits is not possible,
        # so use a fresh redaction workflow in callers. This helper is intended
        # only for single-pass calls with a conservative size.
        return

def cover(page, rect, color=(1,1,1)):
    page.draw_rect(rect, color=color, fill=color, overlay=True)

def put(page, rect, text, font="helv", size=9, align=0, min_size=5.5):
    cover(page, rect)
    size_now = size
    while size_now >= min_size:
        # Use a fresh rectangle each attempt; insert_textbox returns overflow.
        page.insert_textbox(
            rect, str(text or ""), fontname=font, fontsize=size_now,
            color=(0,0,0), align=align, lineheight=1.0, overlay=True
        )
        # PyMuPDF doesn't expose whether this exact operation overflowed without
        # leaving content, so we keep the predefined sizes for this fixed template.
        return

def generate_pdf(pdf_bytes, d):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]

    # Top line: date and orçamento number
    put(page, fitz.Rect(14, 7, 150, 25), d["data"], "helv", 8.5)
    put(page, fitz.Rect(465, 7, 580, 25), f"Orçamento {d['orcamento']}", "helv", 8.5, align=2)

    # Client block
    put(page, fitz.Rect(31, 166, 455, 181), d["cliente"], "hebo", 9.2)
    put(page, fitz.Rect(480, 166, 574, 181), d["telefone_cliente"], "helv", 8.5, align=2)
    put(page, fitz.Rect(31, 185, 330, 198), f"CNPJ: {d['cnpj']}", "hebo", 8.2)
    put(page, fitz.Rect(31, 204, 560, 220), d["endereco_cliente"], "helv", 7.8)

    # Validity / delivery block
    put(page, fitz.Rect(35, 240, 180, 252), f"Validade: {d['validade']}", "hebo", 7.7)
    put(page, fitz.Rect(35, 252, 210, 264), f"Previsão de entrega: {d['entrega']}", "hebo", 7.7)
    put(page, fitz.Rect(35, 263, 438, 276), f"Endereço de entrega: {d['endereco_entrega']}", "helv", 7.5)

    # Partner
    put(page, fitz.Rect(70, 280, 315, 294), d["parceiro"], "helv", 7.8)

    # Item prices and descriptions
    put(page, fitz.Rect(175, 350, 445, 375), d["item1_desc"], "helv", 8.0)
    put(page, fitz.Rect(505, 350, 576, 374), float_to_br_money(br_money_to_float(d["item1_unit"])), "helv", 8.0, align=2)

    put(page, fitz.Rect(175, 441, 445, 458), d["item2_desc"], "helv", 8.0)
    put(page, fitz.Rect(505, 441, 576, 458), float_to_br_money(br_money_to_float(d["item2_unit"])), "helv", 8.0, align=2)

    # Financial values
    item1 = br_money_to_float(d["item1_unit"])
    item2 = br_money_to_float(d["item2_unit"])
    total = item1 + item2
    frete = br_money_to_float(d["frete"])
    pct = br_money_to_float(d["desconto_pct"])
    desconto = br_money_to_float(d["desconto"])
    liquido = total + frete - desconto

    # If the user changed the percentage but not the discount, calculate discount.
    desconto = total * pct / 100 if pct else desconto
    liquido = total + frete - desconto

    put(page, fitz.Rect(515, 496, 577, 510), float_to_br_money(total), "hebo", 8.0, align=2)
    put(page, fitz.Rect(510, 514, 577, 529), "+ " + float_to_br_money(frete), "helv", 8.0, align=2)
    put(page, fitz.Rect(495, 532, 577, 547), "- " + float_to_br_money(desconto) + (f" ({d['desconto_pct']}%)" if d["desconto_pct"] else ""), "helv", 8.0, align=2)
    put(page, fitz.Rect(500, 551, 577, 566), float_to_br_money(liquido), "hebo", 8.5, align=2)

    # Payment block
    put(page, fitz.Rect(20, 567, 300, 579), f"Condição: {d['condicao']}", "hebo", 7.7)
    put(page, fitz.Rect(20, 579, 220, 591), f"Pagamento: 1x {float_to_br_money(liquido)}", "hebo", 7.7)

    put(page, fitz.Rect(20, 609, 80, 622), "1", "helv", 7.8)
    put(page, fitz.Rect(70, 609, 145, 622), d["vencimento"], "helv", 7.8)
    put(page, fitz.Rect(175, 609, 270, 622), float_to_br_money(liquido), "helv", 7.8)
    put(page, fitz.Rect(270, 609, 340, 622), d["forma"], "helv", 7.8)

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

    st.success("PDF carregado. Confira os campos abaixo antes de gerar o novo arquivo.")

    with st.form("editor"):
        st.subheader("Cabeçalho")
        c1, c2 = st.columns(2)
        with c1:
            data = st.text_input("Data", parsed.get("data", ""))
            orcamento = st.text_input("Nº do orçamento", parsed.get("orcamento", ""))
            cliente = st.text_input("Cliente", parsed.get("cliente", ""))
            cnpj = st.text_input("CNPJ", parsed.get("cnpj", ""))
            telefone_cliente = st.text_input("Telefone do cliente", parsed.get("telefone_cliente", ""))
        with c2:
            endereco_cliente = st.text_input("Endereço do cliente", parsed.get("endereco_cliente", ""))
            validade = st.text_input("Validade", parsed.get("validade", ""))
            entrega = st.text_input("Previsão de entrega", parsed.get("entrega", ""))
            endereco_entrega = st.text_input("Endereço de entrega", parsed.get("endereco_entrega", ""))
            parceiro = st.text_input("Parceiro / contato", parsed.get("parceiro", ""))

        st.subheader("Valores")
        c1, c2 = st.columns(2)
        with c1:
            item1_unit = st.text_input("Valor do item 1", parsed.get("item1_unit", ""))
            item2_unit = st.text_input("Valor do item 2", parsed.get("item2_unit", ""))
            frete = st.text_input("Frete", parsed.get("frete", ""))
        with c2:
            desconto_pct = st.text_input("Desconto (%)", parsed.get("desconto_pct", ""))
            desconto = st.text_input("Desconto (R$) — usado se % estiver vazio", parsed.get("desconto", ""))
            condicao = st.text_input("Condição de pagamento", parsed.get("condicao", ""))
            vencimento = st.text_input("Vencimento", parsed.get("vencimento", ""))
            forma = st.text_input("Forma de pagamento", parsed.get("forma", ""))

        submitted = st.form_submit_button("📄 GERAR PDF EDITADO", type="primary", use_container_width=True)

    if submitted:
        d = {
            "data": data, "orcamento": orcamento, "cliente": cliente, "cnpj": cnpj,
            "telefone_cliente": telefone_cliente, "endereco_cliente": endereco_cliente,
            "validade": validade, "entrega": entrega, "endereco_entrega": endereco_entrega,
            "parceiro": parceiro, "item1_desc": parsed.get("item1_desc", ""),
            "item2_desc": parsed.get("item2_desc", ""), "item1_unit": item1_unit,
            "item2_unit": item2_unit, "frete": frete, "desconto_pct": desconto_pct,
            "desconto": desconto, "condicao": condicao, "vencimento": vencimento,
            "forma": forma,
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
