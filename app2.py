from flask import Flask, request, jsonify
import base64
import tempfile
import os
import sys
import subprocess
import zipfile
import urllib.request
import re
import json

app = Flask(__name__)

# --------------------------------------------------------
# Asegura pdftotext en Windows
# --------------------------------------------------------
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

# --------------------------------------------------------
# Extrae texto filtrando páginas
# --------------------------------------------------------
def extract_text(pdf_path):
    try:
        info = subprocess.run(
            ["pdfinfo", pdf_path],
            capture_output=True, text=True, check=True
        ).stdout

        match = re.search(r"Pages:\s+(\d+)", info)
        if not match:
            raise RuntimeError("No se pudo leer número de páginas")

        total_pages = int(match.group(1))
        final_text = ""
        logs = {"total_pages": total_pages, "included_pages": [], "skipped_pages": []}

        for page in range(1, total_pages + 1):
            result = subprocess.run(
                ["pdftotext", "-layout", "-f", str(page), "-l", str(page), pdf_path, "-"],
                capture_output=True, text=True, check=True
            )
            page_text = result.stdout.strip()

            rotate_match = re.search(r"Page\s+{}\s+rotated\s+(\d+)".format(page), info)
            rotation = int(rotate_match.group(1)) if rotate_match else 0
            is_vertical = rotation in (0, 180)

            if "Flight Related Charges" in page_text:
                logs["skipped_pages"].append({"page": page, "reason": "contains 'Flight Related Charges'"})
                continue

            if not is_vertical:
                logs["skipped_pages"].append({"page": page, "reason": f"orientation {rotation}° (landscape)"})
                continue

            logs["included_pages"].append({"page": page, "rotation": rotation})
            final_text += "\n" + page_text

        log_path = pdf_path + "_page_log.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=4)

        print(f"[LOG] Archivo generado: {log_path}")
        print("\n=== TEXTO EXTRAÍDO DEL PDF ===\n")
        print(final_text)
        print("================================\n")

        return final_text, logs

    except Exception as e:
        raise RuntimeError(f"Error extrayendo texto: {e}")

# --------------------------------------------------------
# Endpoint principal
# --------------------------------------------------------
@app.route("/parse-invoice", methods=["POST"])
def parse_invoice():
    temp_path = None
    try:
        if sys.platform.startswith("win"):
            ensure_pdftotext_windows()

        raw_data = request.data

        if raw_data:
            try:
                parsed = json.loads(raw_data)
                if "fileBase64" in parsed:
                    file_bytes = base64.b64decode(parsed["fileBase64"])
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                        tmp.write(file_bytes)
                        temp_path = tmp.name
                    return process_pdf(temp_path)
            except Exception:
                pass

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

# --------------------------------------------------------
# Procesa el PDF y genera JSON
# --------------------------------------------------------
def process_pdf(pdf_path):
    temp_path = pdf_path
    try:
        text, logs = extract_text(temp_path)

        # -----------------------------
        # Campos principales
        # -----------------------------
        invoice_number = re.search(r'Invoice\s*No\.?\s*([A-Z0-9\-]+)', text, re.IGNORECASE)
        invoice_number = invoice_number.group(1) if invoice_number else None

        period_match = re.search(r'Period\s*Covered\s*([\d/]+)\s*-\s*([\d/]+)', text, re.IGNORECASE)
        period_start, period_end = (period_match.group(1), period_match.group(2)) if period_match else (None, None)

        loc_match = re.search(r'Location\s*([A-Z0-9]+)', text, re.IGNORECASE)
        location = loc_match.group(1) if loc_match else None

        subtotal_match = re.search(r'Subtotal\s*([\d,]+\.\d{2})', text, re.IGNORECASE)
        tax_match = re.search(r'Tax\s*([\d,]+\.\d{2})', text, re.IGNORECASE)
        total_match = re.search(r'TOTAL\s*AMOUNT\s*DUE.*?\$?\s*([\d,]+\.\d{2})', text, re.IGNORECASE)

        subtotal = subtotal_match.group(1) if subtotal_match else None
        tax = tax_match.group(1) if tax_match else None
        total = total_match.group(1) if total_match else None

        fees_match = re.search(r'Operating\s*Fees\s*([\d,]+\.\d{2})', text, re.IGNORECASE)
        operating_fees = fees_match.group(1) if fees_match else None

        # --------------------------------------------------------
        # DETALLES — EXTRACCIÓN CON ESPACIO AMPLIO ANTES DEL TOTAL
        # --------------------------------------------------------
        details = []

        # Buscar inicio de detalles
        match_header = re.search(r'DESCRIPTION\s+TOTAL', text)
        if match_header:
            details_text = text[match_header.end():]

            # Cortar antes de la nota final
            end_note_match = re.search(r'Swissport is aware', details_text, re.IGNORECASE)
            if end_note_match:
                details_text = details_text[:end_note_match.start()]

            # Regex para capturar líneas con cualquier texto y un número al final (sin símbolos de $)
            detail_pattern = re.compile(
                r'^(.*?)\s{2,}([\d,]+\.\d{2})\s*$',
                re.MULTILINE
            )

            for m in detail_pattern.finditer(details_text):
                desc = m.group(1).strip()
                total_val = m.group(2).strip()

                # Filtrar líneas no válidas
                bad_keywords = ["subtotal", "tax", "operating", "tax id", "swissport", "total amount due"]
                if any(bad in desc.lower() for bad in bad_keywords):
                    continue

                details.append({
                    "description": desc,
                    "total": total_val
                })

        # --------------------------------------------------------
        # Respuesta final
        # --------------------------------------------------------
        result = {
            "invoice_number": invoice_number,
            "period": {"start": period_start, "end": period_end},
            "location": location,
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
            "operating_fees": operating_fees,
            "details": details,
            "page_log": logs
        }

        if not invoice_number and not details:
            return jsonify({"error": "No data extracted"}), 422

        return jsonify(result)

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

# --------------------------------------------------------
# Run server
# --------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
