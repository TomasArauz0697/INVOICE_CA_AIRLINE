from flask import Flask, request, jsonify
import base64
import tempfile
import os
import sys
import subprocess
import zipfile
import urllib.request
import re

app = Flask(__name__)

# -----------------------------
# Asegura pdftotext en Windows
# -----------------------------
def ensure_pdftotext_windows():
    try:
        subprocess.run(["pdftotext", "-v"], capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("pdftotext no encontrado, descargando Poppler...")
        tmp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(tmp_dir, "poppler.zip")
        url = "https://github.com/oschwartz10612/poppler-windows/releases/download/v23.05.0-0/Release-23.05.0-0.zip"
        urllib.request.urlretrieve(url, zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(tmp_dir)
        poppler_bin = os.path.join(tmp_dir, "poppler-23.05.0", "Library", "bin")
        os.environ["PATH"] = poppler_bin + os.pathsep + os.environ["PATH"]
        subprocess.run(["pdftotext", "-v"], check=True)
        print("pdftotext listo y agregado al PATH temporal.")

# -----------------------------
# Extrae texto del PDF
# -----------------------------
def extract_text(pdf_path):
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, check=True
        )
        return result.stdout
    except Exception as e:
        raise RuntimeError(f"Error extrayendo texto: {e}")

# -----------------------------
# Endpoint principal
# -----------------------------
@app.route("/parse-invoice", methods=["POST"])
def parse_invoice():
    temp_path = None
    try:
        if sys.platform.startswith("win"):
            ensure_pdftotext_windows()

        raw_data = request.data

        # -----------------------------
        # Caso JSON Base64
        # -----------------------------
        if raw_data:
            try:
                import json
                parsed = json.loads(raw_data)
                if "fileBase64" in parsed:
                    file_bytes = base64.b64decode(parsed["fileBase64"])
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                        tmp.write(file_bytes)
                        temp_path = tmp.name
                    return process_pdf(temp_path)
            except Exception:
                pass  # no era JSON

        # -----------------------------
        # Caso multipart/form-data
        # -----------------------------
        if "file" in request.files:
            f = request.files["file"]
            if f.filename == "":
                return jsonify({"error": "Empty filename"}), 400
            suffix = os.path.splitext(f.filename)[1] or ".pdf"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                f.save(tmp.name)
                temp_path = tmp.name
            return process_pdf(temp_path)

        return jsonify({"error": "Send JSON with fileBase64 OR multipart/form-data with file"}), 400

    except Exception as e:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        return jsonify({"error": str(e)}), 500

# -----------------------------
# Procesa el PDF y genera JSON
# -----------------------------
def process_pdf(pdf_path):
    temp_path = pdf_path
    try:
        text = extract_text(temp_path)
        print("\n=== TEXTO EXTRAÍDO DEL PDF ===")
        print(text)
        print("================================\n")

        # -----------------------------
        # Invoice Number
        # -----------------------------
        inv_match = re.search(r'Invoice\s*No\.?\s*([A-Z0-9\-]+)', text, re.IGNORECASE)
        invoice_number = inv_match.group(1) if inv_match else None

        # -----------------------------
        # Period Covered
        # -----------------------------
        period_match = re.search(r'Period\s*Covered\s*([\d/]+)\s*-\s*([\d/]+)', text, re.IGNORECASE)
        period_start, period_end = (period_match.group(1), period_match.group(2)) if period_match else (None, None)

        # -----------------------------
        # Location
        # -----------------------------
        loc_match = re.search(r'Location\s*([A-Z0-9]+)', text, re.IGNORECASE)
        location = loc_match.group(1) if loc_match else None

        # -----------------------------
        # Subtotal, Tax, Total
        # -----------------------------
        subtotal_match = re.search(r'Subtotal\s*([\d,]+\.\d{2})', text, re.IGNORECASE)
        tax_match = re.search(r'Tax\s*([\d,]+\.\d{2})', text, re.IGNORECASE)
        total_match = re.search(r'TOTAL\s*AMOUNT\s*DUE.*?\$?\s*([\d,]+\.\d{2})', text, re.IGNORECASE)

        subtotal = subtotal_match.group(1) if subtotal_match else None
        tax = tax_match.group(1) if tax_match else None
        total = total_match.group(1) if total_match else None

        # -----------------------------
        # Operating Fees / Fees
        # -----------------------------
        fees_match = re.search(r'Operating\s*Fees\s*([\d,]+\.\d{2})', text, re.IGNORECASE)
        operating_fees = fees_match.group(1) if fees_match else None

        # -----------------------------
        # Detalle: descripción y monto
        # -----------------------------
        details = []
        # regex simple: cantidad opcional + descripción + monto al final
        for m in re.finditer(r'(\d*\.*\d*\s*)?([A-Za-z0-9 @\-\&]+?)\s{2,}([\d,]+\.\d{2})', text):
            description, amount = m.group(2).strip(), m.group(3).strip()
            # filtramos líneas vacías y subtotales/totales
            if description.lower() not in ['subtotal', 'tax', 'total', 'operating fees'] and amount != total:
                details.append({"description": description, "amount": amount})

        result = {
            "invoice_number": invoice_number,
            "period": {
                "start": period_start,
                "end": period_end
            },
            "location": location,
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
            "operating_fees": operating_fees,
            "details": details
        }

        if not invoice_number and not details:
            return jsonify({"error": "No data extracted"}), 422

        return jsonify(result)

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

# -----------------------------
# Run server
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
