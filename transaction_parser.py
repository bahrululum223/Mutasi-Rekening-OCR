import re
from datetime import date, datetime
import os

DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
TIME_RE = re.compile(r"^\d{1,2}[.:]\d{2}[.:]\d{2}$")
HEADER_RE = re.compile(r"^(DATE|DATE & TIME|VALUE DATE|DESCRIPTION|REFERENCE NO|DEBIT|CREDIT|SALDO|BALANCE|MUTASI REKENING|TRANSACTION INQUIRY|PERIOD|ACCOUNT NO|CURRENCY|BRANCH|TOTAL TRANSACTION|TOTAL DEBIT|TOTAL CREDIT)\b", re.I)

def parse_number(raw):
    s = raw.strip().replace(",", "")
    try: return float(s)
    except ValueError: return None

def statement_year(text):
    m = re.search(r"\b(?:From|To)\s+\d{1,2}\s+[A-Za-z]+\s+(20\d{2})", text, re.I)
    if m: return int(m.group(1))
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else date.today().year

def normalize_date(raw, year):
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"):
        try: return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError: pass
    return raw

def clean_description(words):
    # preserve natural reading order, avoid duplicate spaces
    lines = []
    for line in words:
        line = re.sub(r"\s+", " ", line).strip(" :")
        if not line or HEADER_RE.match(line): continue
        if re.fullmatch(r"\d+\s*/\s*\d+", line): continue
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()

def parse_mandiri_by_coordinates(filepath, text):
    import pymupdf
    year = statement_year(text)
    transactions = []
    with pymupdf.open(filepath) as doc:
        for page in doc:
            words = page.get_text("words")
            # identify transaction anchor rows by date at left and time immediately beside it
            anchors = []
            for w in words:
                x0, y0, x1, y1, token = w[:5]
                if x0 < 125 and DATE_RE.fullmatch(token):
                    nearby = [v for v in words if abs(v[1] - y0) <= 2 and 65 <= v[0] < 130 and TIME_RE.fullmatch(v[4])]
                    if nearby:
                        anchors.append((y0, token))
            anchors = sorted(set(anchors), key=lambda a: a[0])
            if not anchors: continue

            for i, (y, raw_date) in enumerate(anchors):
                next_y = anchors[i+1][0] if i+1 < len(anchors) else page.rect.height + 1
                block = [w for w in words if y - 1 <= w[1] < next_y - 1]
                # Column positions taken from table geometry, not text extraction order.
                desc_words = [w for w in block if 195 <= w[0] < 455 and w[4].strip()]
                desc_by_y = {}
                for w in desc_words:
                    yy = round(w[1] / 3) * 3
                    desc_by_y.setdefault(yy, []).append(w)
                desc_lines = []
                for yy in sorted(desc_by_y):
                    desc_lines.append(" ".join(w[4] for w in sorted(desc_by_y[yy], key=lambda z:z[0])))
                description = clean_description(desc_lines)

                def col_amount(xmin, xmax):
                    vals=[]
                    for w in block:
                        x0,y0,x1,y1,token = w[:5]
                        if xmin <= x0 < xmax and re.fullmatch(r"\d[\d,]*\.\d{2}", token):
                            val=parse_number(token)
                            if val is not None: vals.append((y0,val,token))
                    # column values can appear on the same line as anchor, take first value in the transaction block
                    return vals[0][1] if vals else None

                debit = col_amount(440, 530)
                credit = col_amount(530, 595)
                balance = col_amount(595, 700)
                debit = None if debit is None or abs(debit) < 0.0001 else debit
                credit = None if credit is None or abs(credit) < 0.0001 else credit

                review = "OK"
                if not description:
                    description = "(deskripsi tidak terbaca)"
                    review = "Perlu review: deskripsi"
                if debit is None and credit is None:
                    review = "Perlu review: nominal tidak terbaca"
                if balance is None:
                    review = "Perlu review: saldo tidak terbaca"

                transactions.append({
                    "Date": normalize_date(raw_date, year),
                    "Description": description,
                    "Debit": debit,
                    "Credit": credit,
                    "Balance": balance,
                    "Review": review
                })

    # Validate the balances between consecutive rows. If no opening balance is available,
    # check from the second transaction onwards.
    for i in range(1, len(transactions)):
        prev = transactions[i-1]
        row = transactions[i]
        if prev["Balance"] is not None and row["Balance"] is not None:
            expected = prev["Balance"] - (row["Debit"] or 0) + (row["Credit"] or 0)
            if abs(expected - row["Balance"]) > 0.02 and row["Review"] == "OK":
                row["Review"] = "Cek saldo"

    return transactions

def parse_generic(text, year):
    # Generic fallback for statements with one transaction per date-led row.
    rows, current = [], None
    date_line = re.compile(r"^\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+(.*)$")
    money_re = re.compile(r"(?<![\d/])\(?-?\d[\d,]*\.\d{2}\)?(?!\d)")
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line: continue
        m = date_line.match(line)
        if m and not HEADER_RE.match(line):
            if current: rows.append(current)
            d, rest = m.groups()
            tokens = [(x, parse_number(x.replace("(", "-").replace(")", ""))) for x in money_re.findall(rest)]
            debit = credit = balance = None
            if len(tokens) >= 3:
                debit, credit, balance = [v for _,v in tokens[-3:]]
                debit = debit or None
                credit = credit or None
                rest = rest[:rest.find(tokens[-3][0])].strip()
            current = {"Date": normalize_date(d, year), "Description": rest,
                       "Debit": debit, "Credit": credit, "Balance": balance,
                       "Review": "Perlu review: parser generik"}
        elif current and not HEADER_RE.match(line):
            current["Description"] = (current["Description"] + " " + line).strip()
    if current: rows.append(current)
    for r in rows:
        r["Description"] = clean_description([r["Description"]])
        if r["Debit"] is None and r["Credit"] is None:
            r["Review"] = "Perlu review: nominal tidak terbaca"
    return rows


def parse_bca_pdf(filepath, text):
    """Parser for BCA Rekening Tahapan PDF with multi-line transactions."""
    import pymupdf
    rows = []
    month_match = re.search(r"PERIODE\s*:?\s*([A-Z]+)\s+(20\d{2})", text, re.I)
    month_names = {"JANUARI":1,"FEBRUARI":2,"MARET":3,"APRIL":4,"MEI":5,"JUNI":6,
                   "JULI":7,"AGUSTUS":8,"SEPTEMBER":9,"OKTOBER":10,"NOVEMBER":11,"DESEMBER":12}
    month = month_names.get(month_match.group(1).upper(), 9) if month_match else 9
    year = int(month_match.group(2)) if month_match else statement_year(text)
    date_start = re.compile(r"^\s*(\d{2})/(\d{2})\s+(.*?)\s*$")
    money_re = re.compile(r"(?<![\d/])(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?!\d)")
    header_re = re.compile(r"^(REKENING TAHAPAN|KCU TEGAL|NO\. REKENING|HALAMAN|PERIODE|MATA UANG|CATATAN|TANGGAL|KETERANGAN|CBG|MUTASI|SALDO|BERSAMBUNG)", re.I)

    with pymupdf.open(filepath) as pdf:
        for page in pdf:
            lines = [re.sub(r"\s+", " ", x).strip()
                     for x in page.get_text("text", sort=True).splitlines() if x.strip()]
            blocks, current = [], None
            for line in lines:
                match = date_start.match(line)
                if match:
                    if current:
                        blocks.append(current)
                    current = {"date": match.group(1) + "/" + match.group(2),
                               "lines": [match.group(3)]}
                elif current and not line.lower().startswith("bersambung"):
                    if re.match(r"^(SALDO AWAL|MUTASI CR|MUTASI DB|SALDO AKHIR)\b", line, re.I):
                        if current:
                            blocks.append(current)
                        current = None
                        continue
                    current["lines"].append(line)
            if current:
                blocks.append(current)

            for block in blocks:
                raw = "\n".join(block["lines"])
                if re.search(r"\bSALDO AWAL\b", raw, re.I):
                    continue

                # Ignore footer summaries and pages without actual transaction amounts.
                is_debit = bool(re.search(r"\bDB\b", raw, re.I))
                txn_type = "debit" if is_debit else "credit"

                # Prefer currency amounts with thousands separators. Unformatted numbers
                # such as 2505000.00 often repeat transfer values in the reference column.
                tokens = []
                for match in money_re.finditer(raw):
                    token = match.group(0)
                    if "," not in token and not re.search(r"\b(BUNGA|PAJAK BUNGA|BIAYA ADM)\b", raw, re.I):
                        continue
                    tokens.append((token, float(token.replace(",", ""))))

                amount = balance = None
                if len(tokens) >= 2:
                    amount, balance = tokens[-2][1], tokens[-1][1]
                elif len(tokens) == 1:
                    amount = tokens[0][1]
                elif re.search(r"\b(BUNGA|PAJAK BUNGA|BIAYA ADM)\b", raw, re.I):
                    values = re.findall(r"(?<![\d/])\d+\.\d{2}(?!\d)", raw)
                    if values:
                        amount = float(values[-1].replace(",", ""))

                # Remove numeric amount strings and DB markers from description.
                desc_lines = []
                for line in block["lines"]:
                    cleaned = money_re.sub(" ", line)
                    cleaned = re.sub(r"\bDB\b", " ", cleaned)
                    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:")
                    if cleaned and not re.fullmatch(r"[\d\s:/.-]+", cleaned) and not header_re.match(cleaned):
                        desc_lines.append(cleaned)
                description = re.sub(r"\s+", " ", " ".join(desc_lines)).strip()

                if amount is None:
                    continue
                day, txn_month = map(int, block["date"].split("/"))
                try:
                    iso_date = date(year, txn_month, day).isoformat()
                except ValueError:
                    iso_date = block["date"]

                rows.append({
                    "Date": iso_date,
                    "Description": description or "(deskripsi tidak terbaca)",
                    "Debit": amount if is_debit else None,
                    "Credit": None if is_debit else amount,
                    "Balance": balance,
                    "Review": "OK" if description else "Perlu review: deskripsi"
                })

    # Check row-to-row balance only where consecutive balances are available.
    for i in range(1, len(rows)):
        prev, row = rows[i-1], rows[i]
        if prev["Balance"] is not None and row["Balance"] is not None:
            expected = prev["Balance"] - (row["Debit"] or 0) + (row["Credit"] or 0)
            if abs(expected - row["Balance"]) > 0.02 and row["Review"] == "OK":
                row["Review"] = "Cek saldo"
    return rows



def parse_bni_scan(text):
    """Parser for scanned BNI Taplus Bisnis statements using OCR text."""
    month_map = {
        "JAN": 1, "JANUARI": 1, "FEB": 2, "FEBRUARI": 2, "MAR": 3, "MARET": 3,
        "APR": 4, "APRIL": 4, "MEI": 5, "MAY": 5, "JUN": 6, "JUNI": 6,
        "JUL": 7, "JULI": 7, "AGU": 8, "AGUSTUS": 8, "AUG": 8,
        "SEP": 9, "SEPTEMBER": 9, "OKT": 10, "OKTOBER": 10, "OCT": 10,
        "NOV": 11, "NOVEMBER": 11, "DES": 12, "DESEMBER": 12, "DEC": 12
    }
    period_year = 2024
    m = re.search(r"(?:PERIODE\s*TGL|S/D)\s*:?\s*\d{1,2}/\d{1,2}/(20\d{2})", text, re.I)
    if m:
        period_year = int(m.group(1))
    else:
        m = re.search(r"\b(20\d{2})\b", text)
        if m: period_year = int(m.group(1))

    date_line = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]{3,9})\b\s*(.*)$", re.I)
    # Indonesian amount format, including OCR spaces before decimals and trailing minus.
    money_re = re.compile(r"(?<![\w/])(\d[\d.]*,\s*\d{2}\s*-?)(?!\w)")
    rows = []
    current = None

    def finish(item):
        if not item: return
        raw_lines = item["lines"]
        joined = " ".join(raw_lines)
        amounts = []
        for mt in money_re.finditer(joined):
            token = mt.group(1)
            normalized = re.sub(r"\s+", "", token)
            trailing_minus = normalized.endswith("-")
            normalized = normalized.rstrip("-").replace(".", "").replace(",", ".")
            try:
                val = float(normalized)
            except ValueError:
                continue
            amounts.append((mt.start(), mt.end(), val, trailing_minus, token))
        is_opening = bool(re.search(r"SALDO\s+AWAL", joined, re.I))
        if is_opening:
            balance = amounts[-1][2] if amounts else None
            rows.append({
                "Date": "",
                "Description": "SALDO AWAL",
                "Debit": None, "Credit": None, "Balance": balance,
                "Review": "OK" if balance is not None else "Perlu review: saldo awal"
            })
            return
        if not amounts:
            return
        # The last monetary value is the running balance; preceding final value is transaction amount.
        balance = amounts[-1][2] if len(amounts) >= 2 else None
        txn_amount_tuple = amounts[-2] if len(amounts) >= 2 else amounts[-1]
        amount = txn_amount_tuple[2]
        is_debit = txn_amount_tuple[3]
        # Remove money values and date from description, but keep codes / references.
        desc = joined
        desc = money_re.sub(" ", desc)
        desc = re.sub(r"\s+", " ", desc).strip(" -:")
        desc = re.sub(r"\b(?:TGL|TRANS|DEBET|KREDIT|SALDO)\b", " ", desc, flags=re.I)
        desc = re.sub(r"\s+", " ", desc).strip(" -:")
        try:
            day = int(item["day"])
            month = month_map.get(item["month"].upper(), 12)
            iso_date = date(period_year, month, day).isoformat()
        except Exception:
            iso_date = f'{item["day"]} {item["month"]} {period_year}'
        rows.append({
            "Date": iso_date,
            "Description": desc or "(deskripsi tidak terbaca)",
            "Debit": amount if is_debit else None,
            "Credit": None if is_debit else amount,
            "Balance": balance,
            "Review": "OK" if desc and balance is not None else "Perlu review: periksa nominal/deskripsi"
        })

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if re.search(r"AKHIR DARI REKENING", line, re.I):
            finish(current); current = None
            break
        if re.search(r"\bSALDO\s+AWAL\b", line, re.I):
            if current: finish(current)
            current = {"day": "", "month": "", "lines": [line]}
            continue
        m = date_line.match(line)
        if m and m.group(2).upper()[:3] in month_map:
            if current: finish(current)
            current = {"day": m.group(1), "month": m.group(2), "lines": [m.group(3)]}
        elif current:
            # Ignore page headers/column headings if OCR includes them mid-document.
            if not re.match(r"^(TGL|TRANS|URAIAN|DEBET|KREDIT|SALDO|CABANG|REKENING|PERIODE|NO\. REKENING|NPWP|MATAUANG|HAL:)", line, re.I):
                current["lines"].append(line)
    if current: finish(current)

    # Verify balance progression using previous available balance.
    for i in range(1, len(rows)):
        prev, row = rows[i-1], rows[i]
        if prev.get("Balance") is None or row.get("Balance") is None:
            continue
        expected = prev["Balance"] - (row.get("Debit") or 0) + (row.get("Credit") or 0)
        if abs(expected - row["Balance"]) > 0.02 and row["Review"] == "OK":
            row["Review"] = "Cek saldo"
    return rows


def parse_transactions(text, filepath=None):
    year = statement_year(text)
    rows = []
    is_bni = bool(re.search(r"REKENING\s*:\s*TAPLUS|TAPLUS BISNIS|CABANG\s*:\s*INDRAMAYU", text, re.I)
                  and re.search(r"DEBET.*KREDIT.*SALDO", text, re.I | re.S))
    is_bca = bool(re.search(r"REKENING TAHAPAN", text, re.I) and
                  re.search(r"MUTASI CR", text, re.I) and
                  re.search(r"MUTASI DB", text, re.I))
    is_mandiri = bool(re.search(r"TRANSACTION INQUIRY", text, re.I) and
                      re.search(r"Date\s*&\s*Time", text, re.I))
    if is_bni:
        rows = parse_bni_scan(text)
    elif filepath and os.path.splitext(filepath)[1].lower() == ".pdf" and is_bca:
        rows = parse_bca_pdf(filepath, text)
    elif filepath and os.path.splitext(filepath)[1].lower() == ".pdf" and is_mandiri:
        try:
            rows = parse_mandiri_by_coordinates(filepath, text)
        except Exception:
            rows = []
    if not rows:
        rows = parse_generic(text, year)

    summary = {
        "count": len([r for r in rows if r.get("Description") != "SALDO AWAL"]),
        "debit": round(sum(float(r.get("Debit") or 0) for r in rows), 2),
        "credit": round(sum(float(r.get("Credit") or 0) for r in rows), 2),
        "closing_balance": next((r.get("Balance") for r in reversed(rows) if r.get("Balance") is not None), None)
    }
    return rows, summary
