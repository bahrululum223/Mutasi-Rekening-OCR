import os
import pandas as pd

COLUMNS = ["Date", "Description", "Debit", "Credit", "Balance"]

def export_transactions(transactions, output_directory="output", summary=None, filename_prefix="result"):
    os.makedirs(output_directory, exist_ok=True)
    df = pd.DataFrame(transactions)
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[COLUMNS]
    xlsx_path = os.path.join(output_directory, f"{filename_prefix}_mutasi_rekening.xlsx")
    csv_path = os.path.join(output_directory, f"{filename_prefix}_mutasi_rekening.csv")
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Transaksi")
        ws = writer.sheets["Transaksi"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col, width in {"A":14,"B":52,"C":18,"D":18,"E":18}.items():
            ws.column_dimensions[col].width = width
        for col in ("C", "D", "E"):
            for cell in ws[col][1:]:
                cell.number_format = '#,##0.00;[Red]-#,##0.00'
        if summary:
            s = pd.DataFrame([
                ["Jumlah transaksi", summary["count"]],
                ["Total debit", summary["debit"]],
                ["Total kredit", summary["credit"]],
                ["Saldo akhir", summary["closing_balance"]]
            ], columns=["Ringkasan", "Nilai"])
            s.to_excel(writer, index=False, sheet_name="Ringkasan")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return xlsx_path, csv_path
