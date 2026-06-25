from __future__ import annotations

from io import BytesIO

import pandas as pd


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def to_excel_bytes(
    df: pd.DataFrame, sheet_name: str = "Report", summary: dict | None = None
) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        start_row = 0
        if summary:
            pd.DataFrame(
                [{"Metric": key, "Value": value} for key, value in summary.items()]
            ).to_excel(writer, index=False, sheet_name="Summary")
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31], startrow=start_row)
        worksheet = writer.sheets[sheet_name[:31]]
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for column_cells in worksheet.columns:
            width = min(
                max(len(str(cell.value or "")) for cell in column_cells) + 2, 40
            )
            worksheet.column_dimensions[column_cells[0].column_letter].width = width
    return output.getvalue()

