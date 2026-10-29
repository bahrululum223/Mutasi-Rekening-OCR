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



# Universal fallback helpers. These heuristics intentionally flag uncertain rows for review.
DATE_TOKEN = r"(?:\d{8}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}\s+[A-Za-zÀ-ÿ]{3,12}\s+\d{2,4})"
DATE_LINE_RE = re.compile(r"^\s*(" + DATE_TOKEN + r")\b\s*(.*)$", re.I)
AMOUNT_RE = re.compile(r"(?<![\w/])(?:\(?-?\s*(?:\d{1,3}(?:[.,\s]\d{3})+|\d+)(?:[.,]\d{1,6})?\)?)(?:\s*(?:DB|DR|CR|D|C))?(?!\w)", re.I)
MONTHS = {
    "JAN":1,"JANUARI":1,"JANUARY":1,"FEB":2,"FEBRUARI":2,"FEBRUARY":2,
    "MAR":3,"MARET":3,"MARCH":3,"APR":4,"APRIL":4,"MEI":5,"MAY":5,
    "JUN":6,"JUNI":6,"JUNE":6,"JUL":7,"JULI":7,"JULY":7,"AGU":8,
    "AGUSTUS":8,"AUG":8,"AUGUST":8,"SEP":9,"SEPT":9,"SEPTEMBER":9,
    "OCT":10,"OKT":10,"OKTOBER":10,"OCTOBER":10,"NOV":11,"NOVEMBER":11,
    "DEC":12,"DES":12,"DESEMBER":12,"DECEMBER":12
}

def _parse_flexible_date(raw, year_hint):
    value = re.sub(r"\s+", " ", (raw or "").strip().strip(":"))
    if re.fullmatch(r"\d{8}", value):
        try:
            return datetime.strptime(value, "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d/%m/%Y", "%d-%m-%Y",
                "%m/%d/%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{2,4})", value, re.I)
    if m:
        month = MONTHS.get(m.group(2).upper())
        if month:
            yr = int(m.group(3)); yr += 2000 if yr < 100 else 0
            try: return date(yr, month, int(m.group(1))).isoformat()
            except ValueError: pass
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", value)
    if m:
        a, b = map(int, m.groups())
        # Prefer day/month for Indonesian bank statements; ambiguity is flagged downstream.
        try: return date(year_hint, b, a).isoformat()
        except ValueError: pass
    return value

def _parse_flexible_amount(token):
    """Parse common Indonesian/international amounts without rounding."""
    raw = re.sub(r"\s+", "", str(token or "")).upper()
    suffix = ""
    m = re.search(r"(DB|DR|CR|D|C)$", raw)
    if m:
        suffix = m.group(1); raw = raw[:m.start()]
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    if raw.startswith("-"):
        negative = True
    raw = raw.lstrip("+-")
    if not raw or not re.search(r"\d", raw): return None, suffix
    raw = re.sub(r"[^0-9,.]", "", raw)
    if "," in raw and "." in raw:
        # Last separator is decimal separator when followed by 1-2 digits;
        # otherwise separators are treated as thousands separators.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        parts = raw.split(",")
        if len(parts[-1]) in (1, 2) and len(parts) == 2:
            raw = parts[0].replace(".", "") + "." + parts[1]
        elif len(parts[-1]) == 3:
            raw = "".join(parts)
        else:
            raw = "".join(parts[:-1]) + ("." + parts[-1] if len(parts[-1]) <= 2 else parts[-1])
    elif raw.count(".") > 1:
        parts = raw.split(".")
        if len(parts[-1]) in (1, 2):
            raw = "".join(parts[:-1]) + "." + parts[-1]
        else:
            raw = "".join(parts)
    elif "." in raw:
        left, right = raw.rsplit(".", 1)
        if len(right) == 3 and left.isdigit():
            raw = left + right
    try:
        val = float(raw)
        if negative or suffix in ("DB", "DR", "D"): val = abs(val)
        return val, suffix
    except ValueError:
        return None, suffix

def _header_kind(line):
    u = line.upper()
    if not re.search(r"\b(TANGGAL|DATE|TGL|TRANSACTION DATE|POSTING DATE)\b", u): return None
    if not re.search(r"\b(KETERANGAN|DESCRIPTION|URAIAN|DETAIL|TRANSAKSI|MUTASI|DEBIT|DEBET|KREDIT|CREDIT|BALANCE|SALDO|AMOUNT|JUMLAH)\b", u): return None
    return {
        "debit": bool(re.search(r"\b(DEBIT|DEBET|DB|WITHDRAWAL|OUT)\b", u)),
        "credit": bool(re.search(r"\b(CREDIT|KREDIT|CR|DEPOSIT|IN)\b", u)),
        "balance": bool(re.search(r"\b(BALANCE|SALDO)\b", u)),
        "amount": bool(re.search(r"\b(AMOUNT|JUMLAH|MUTASI)\b", u)),
    }

def parse_universal(text, year):
    """Bank-agnostic fallback for date-led transaction rows and common text tables."""
    rows, current = [], None
    header_seen = False
    header_flags = {"debit": False, "credit": False, "balance": False, "amount": False}
    skip_re = re.compile(
        r"^(TANGGAL|TGL|DATE|DESCRIPTION|KETERANGAN|URAIAN|SALDO|BALANCE|DEBIT|DEBET|KREDIT|CREDIT|"
        r"MUTASI REKENING|ACCOUNT|REKENING|NO\.?\s*REKENING|PERIODE|PERIOD|CURRENCY|MATA UANG|"
        r"HALAMAN|PAGE|OPENING BALANCE|CLOSING BALANCE|SALDO AWAL|SALDO AKHIR|TOTAL|SUBTOTAL|"
        r"BERSAMBUNG|CATATAN|STATEMENT)\b", re.I)

    def finish(item):
        if not item: return
        raw = " ".join(item["lines"]).strip()
        if not raw: return
        # Strip repeating page headers/footers and keep all non-numeric description material.
        amounts = []
        for match in AMOUNT_RE.finditer(raw):
            token = match.group(0).strip()
            value, suffix = _parse_flexible_amount(token)
            if value is not None:
                amounts.append((match.start(), match.end(), value, suffix, token))
        if not amounts and not item["date"]:
            return
        description = raw
        for start, end, _, _, _ in reversed(amounts):
            description = description[:start] + " " + description[end:]
        description = re.sub(r"\b(?:DB|DR|CR)\b", " ", description, flags=re.I)
        description = re.sub(r"\s+", " ", description).strip(" -|:;")
        if skip_re.match(description): return

        debit = credit = balance = None
        review = "Perlu diperiksa"
        if len(amounts) >= 3 and header_flags["debit"] and header_flags["credit"]:
            # Table has distinct debit and credit columns; use last three values cautiously.
            debit, credit, balance = amounts[-3][2], amounts[-2][2], amounts[-1][2]
            debit = debit or None; credit = credit or None
        elif len(amounts) >= 2:
            txn = amounts[-2]
            balance = amounts[-1][2]
            value, suffix = txn[2], txn[3]
            upper = raw.upper()
            if suffix in ("DB", "DR", "D") or re.search(r"\b(DEBIT|DEBET|WITHDRAWAL|PAYMENT|TRANSFER OUT|PURCHASE|BIAYA|TARIK TUNAI|MONTHLY FEE|ADMIN FEE)\b", upper):
                debit = value
            elif suffix in ("CR", "C") or re.search(r"\b(CREDIT|KREDIT|DEPOSIT|TRANSFER IN|INCOMING|SETORAN|BUNGA|REFUND|SALARY|GAJI|PENERIMAAN)\b", upper):
                credit = value
            else:
                # No reliable debit/credit indicator: keep the value in the likely column only
                # when the header provides a clear cue; otherwise require human review.
                if header_flags["debit"] and not header_flags["credit"]: debit = value
                elif header_flags["credit"] and not header_flags["debit"]: credit = value
                else: debit = value
        elif len(amounts) == 1:
            value, suffix = amounts[0][2], amounts[0][3]
            if suffix in ("CR", "C"): credit = value
            else: debit = value

        if not description: description = "(deskripsi tidak terbaca)"
        if debit is not None or credit is not None:
            review = "Perlu diperiksa"
        if not amounts:
            review = "Perlu diperiksa: nominal tidak terbaca"
        rows.append({
            "Date": _parse_flexible_date(item["date"], year),
            "Description": description,
            "Debit": debit,
            "Credit": credit,
            "Balance": balance,
            "Review": review
        })

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line: continue
        flags = _header_kind(line)
        if flags:
            header_seen = True
            for key, value in flags.items():
                header_flags[key] = header_flags[key] or value
            continue
        if skip_re.match(line):
            continue
        match = DATE_LINE_RE.match(line)
        if match:
            d, rest = match.groups()
            # Avoid treating reference numbers or dates inside prose as transaction starts.
            parsed_date = _parse_flexible_date(d, year)
            if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", parsed_date) or re.match(r"^\d{1,2}[-/]\d{1,2}", d) or re.match(r"^\d{1,2}\s+[A-Za-zÀ-ÿ]+", d):
                if current: finish(current)
                current = {"date": d, "lines": [rest] if rest else []}
                continue
        if current:
            # Do not append recurring headers/footers; multiline descriptions are common.
            if not skip_re.match(line) and not re.fullmatch(r"\d+\s*/\s*\d+", line):
                current["lines"].append(line)
    if current: finish(current)

    # Deduplicate adjacent OCR repeats conservatively.
    deduped = []
    seen = set()
    for row in rows:
        key = (row["Date"], row["Description"], row["Debit"], row["Credit"], row["Balance"])
        if key not in seen:
            deduped.append(row); seen.add(key)
    return deduped




def parse_cimb_statement(text):
    """Parse CIMB Niaga account statements where transaction rows use DD/MM dates."""
    rows = []
    money = re.compile(r'(?<![\d/])(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?!\d)')
    year = statement_year(text)
    opening_match = re.search(r'SALDO\s+AWAL\s+([\d,]+\.\d{2})', text, re.I)
    previous_balance = float(opening_match.group(1).replace(',', '')) if opening_match else None
    current = None

    def finish(item):
        nonlocal previous_balance
        if not item:
            return
        raw = " ".join(item["lines"])
        vals = [float(v.replace(",", "")) for v in money.findall(raw)]
        if not vals:
            return
        # In the source statement, each transaction line contains its non-empty
        # debit/credit amount and final balance; continuation lines carry references.
        balance = vals[-1]
        amount = vals[-2] if len(vals) >= 2 else None
        if amount is None:
            return
        delta = round(balance - previous_balance, 2) if previous_balance is not None else None
        debit = credit = None
        review = "Perlu diperiksa"
        if delta is not None:
            if delta < -0.005:
                debit = abs(delta)
            elif delta > 0.005:
                credit = delta
            if abs(abs(delta) - amount) <= 0.03:
                review = "OK"
            else:
                review = "Perlu diperiksa: nominal tidak cocok dengan saldo"
        desc = re.sub(r'\s+', ' ', money.sub(' ', raw)).strip(' -:|')
        desc = re.sub(r'\b(?:MISCELLANEOUS CHARGES|REMITTANCE CR - SKN|INCOMING SKN|BILL PAYMENT|TR TO REMITT)\b', ' ', desc, flags=re.I)
        desc = re.sub(r'\s+', ' ', desc).strip(' -:|')
        d, m = map(int, item["date"].split('/'))
        try: iso_date = date(year, m, d).isoformat()
        except ValueError: iso_date = item["date"]
        rows.append({"Date": iso_date, "Description": desc or "(deskripsi tidak terbaca)",
                     "Debit": debit, "Credit": credit, "Balance": balance, "Review": review})
        previous_balance = balance

    for raw_line in text.splitlines():
        line = re.sub(r'\s+', ' ', raw_line).strip()
        if not line:
            continue
        if re.match(r'^(TOTAL|SALDO AKHIR|DISCLAIMER|TERIMA KASIH|THANK YOU|HALAMAN|PAGE)\b', line, re.I):
            finish(current); current = None
            continue
        m = re.match(r'^(\d{2})/(\d{2})\s+(\d{2})/(\d{2})\s+(.*)$', line)
        if m:
            finish(current)
            current = {"date": f"{m.group(1)}/{m.group(2)}", "lines": [m.group(5)]}
        elif current and not re.match(r'^(TGL\.?\s*TXN|TXN\.?\s*DATE|TGL\.?\s*VALUTA|DESCRIPTION|DEBET|DEBIT|KREDIT|CREDIT|SALDO|BALANCE)\b', line, re.I):
            current["lines"].append(line)
    finish(current)
    return rows


def parse_jtrust_statement(text):
    """Parse J Trust statement rows with YYYYMMDD dates and separate debit/credit columns."""
    money = re.compile(r'(?<![\d/])\d[\d,]*\.\d{2}(?!\d)')
    rows, current = [], None
    year_hint = statement_year(text)
    opening_match = re.search(r"SALDO\s+AWAL\s+([\d,]+\.\d{2})", text, re.I)
    opening_balance = float(opening_match.group(1).replace(",", "")) if opening_match else None

    def finish(item):
        if not item:
            return
        raw = " ".join(item["lines"])
        vals = [float(x.replace(",", "")) for x in money.findall(raw)]
        if len(vals) < 2:
            return
        # J Trust layout: description/reference, debit, credit, balance.
        # Empty debit/credit cells collapse in extracted text, so use balance movement
        # to classify the amount after the first row.
        amount, balance = vals[-2], vals[-1]
        rows.append({
            "Date": _parse_flexible_date(item["date"], year_hint),
            "Description": re.sub(r"\s+", " ", money.sub(" ", raw)).strip(" -:") or "(deskripsi tidak terbaca)",
            "Debit": None, "Credit": None, "Balance": balance,
            "Review": "Perlu diperiksa",
            "_amount": amount
        })

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if re.search(r"\bSALDO AWAL\b", line, re.I):
            continue
        if re.search(r"\bSALDO AKHIR\b", line, re.I):
            finish(current); current = None
            continue
        m = re.match(r"^(\d{8})\s+(\d{8})\s+(.*)$", line)
        if m:
            finish(current)
            current = {"date": m.group(1), "lines": [m.group(3)]}
        elif current and not re.match(r"^(TANGGAL|VALUTA|KETERANGAN|NOMOR REFERENSI|MUTASI DEBET|MUTASI KREDIT|SALDO|HALAMAN|PERIODE|CATATAN)\b", line, re.I):
            current["lines"].append(line)
    finish(current)

    prev_balance = opening_balance
    for row in rows:
        amount, balance = row.pop("_amount"), row["Balance"]
        if prev_balance is not None and balance is not None:
            delta = round(balance - prev_balance, 2)
            if delta < -0.005:
                row["Debit"] = abs(delta)
            elif delta > 0.005:
                row["Credit"] = delta
            else:
                row["Review"] = "Perlu diperiksa: mutasi tidak terdeteksi"
            if abs(abs(delta) - amount) > 0.02:
                row["Review"] = "Perlu diperiksa: nominal berbeda dari perubahan saldo"
            else:
                row["Review"] = "OK"
        else:
            row["Review"] = "Perlu diperiksa: saldo sebelumnya tidak tersedia"
        prev_balance = balance
    return rows


def parse_bri_ocr(text):
    """Parse scanned BRI financial transaction statements after OCR."""
    rows = []
    # OCR commonly preserves a transaction as date/time, description, debit, credit, balance.
    line_re = re.compile(r'^(\d{2}/\d{2}/\d{2,4})\s+(\d{2}:\d{2}:\d{2})\s+(.*)$')
    money = re.compile(r'(?<![\w/])(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?!\d)')
    year = statement_year(text)
    current = None

    def finish(item):
        if not item:
            return
        raw = " ".join(item["lines"])
        vals = [float(x.replace(",", "")) for x in money.findall(raw)]
        if len(vals) < 3:
            return
        debit_amt, credit_amt, balance = vals[-3], vals[-2], vals[-1]
        desc = money.sub(" ", raw)
        desc = re.sub(r'\b\d{6,}\b', ' ', desc)
        desc = re.sub(r'\s+', ' ', desc).strip(" -:")
        try:
            txn_date = datetime.strptime(item["date"] + " " + item["time"], "%d/%m/%y %H:%M:%S").date().isoformat()
        except ValueError:
            try: txn_date = datetime.strptime(item["date"] + " " + item["time"], "%d/%m/%Y %H:%M:%S").date().isoformat()
            except ValueError: txn_date = item["date"]
        debit = debit_amt if debit_amt > 0 else None
        credit = credit_amt if credit_amt > 0 else None
        rows.append({"Date": txn_date, "Description": desc or "(deskripsi tidak terbaca)",
                     "Debit": debit, "Credit": credit, "Balance": balance,
                     "Review": "Perlu diperiksa"})

    for raw_line in text.splitlines():
        line = re.sub(r'\s+', ' ', raw_line).strip()
        if (re.search(r'SALDO AWAL.*TOTAL TRANSAKSI|TOTAL TRANSAKSI.*SALDO AKHIR', line, re.I)
                or re.match(r'^(SALDO AWAL|OPENING BALANCE|TOTAL TRANSAKSI|TOTAL DEBIT|TOTAL KREDIT|SALDO AKHIR|CLOSING BALANCE|TERBILANG|HALAMAN|PAGE)\b', line, re.I)):
            finish(current); current = None
            continue
        m = line_re.match(line)
        if m:
            finish(current)
            current = {"date": m.group(1), "time": m.group(2), "lines": [m.group(3)]}
        elif current:
            current["lines"].append(line)
    finish(current)
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
    is_jtrust = bool(re.search(r"JTRUST|J TRUST BANK|MUTASI DEBET.*MUTASI KREDIT", text, re.I | re.S)
                     and re.search(r"\b\d{8}\s+\d{8}\s+", text))
    is_bri = bool(re.search(r"BRITAMA BISNIS|LAPORAN TRANSAKSI FINANSIAL|STATEMENT OF FINANCIAL TRANSACTION", text, re.I)
                  and re.search(r"\d{2}/\d{2}/\d{2,4}\s+\d{2}:\d{2}:\d{2}", text))
    is_cimb = bool(re.search(r"CIMB NIAGA|LAPORAN TRANSAKSI", text, re.I)
                   and re.search(r"TXN\.\s*DATE.*DESCRIPTION.*DEBIT.*CREDIT.*BALANCE", text, re.I | re.S))
    if is_jtrust:
        rows = parse_jtrust_statement(text)
    elif is_cimb:
        rows = parse_cimb_statement(text)
    elif is_bri:
        rows = parse_bri_ocr(text)
    elif is_bni:
        rows = parse_bni_scan(text)
    elif filepath and os.path.splitext(filepath)[1].lower() == ".pdf" and is_bca:
        rows = parse_bca_pdf(filepath, text)
    elif filepath and os.path.splitext(filepath)[1].lower() == ".pdf" and is_mandiri:
        try:
            rows = parse_mandiri_by_coordinates(filepath, text)
        except Exception:
            rows = []
    if not rows:
        rows = parse_universal(text, year)
    # Universal parser is also a secondary recovery path if a bank-specific parser
    # found only a small fragment of a longer statement.
    if rows and len(rows) < 2 and len(re.findall(DATE_TOKEN, text, re.I)) >= 2:
        recovered = parse_universal(text, year)
        if len(recovered) > len(rows):
            rows = recovered

    summary = {
        "count": len([r for r in rows if r.get("Description") != "SALDO AWAL"]),
        "debit": round(sum(float(r.get("Debit") or 0) for r in rows), 2),
        "credit": round(sum(float(r.get("Credit") or 0) for r in rows), 2),
        "closing_balance": next((r.get("Balance") for r in reversed(rows) if r.get("Balance") is not None), None)
    }
    return rows, summary
