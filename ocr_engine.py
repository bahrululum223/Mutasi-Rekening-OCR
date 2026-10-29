import os
from io import BytesIO
import pymupdf
import pytesseract
from PIL import Image, ImageOps, ImageEnhance, ImageFilter

TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]
for candidate in TESSERACT_CANDIDATES:
    if os.path.exists(candidate):
        pytesseract.pytesseract.tesseract_cmd = candidate
        break

def _ocr(image):
    image = ImageOps.grayscale(image)
    image = ImageEnhance.Contrast(image).enhance(1.7)
    image = image.filter(ImageFilter.SHARPEN)
    return pytesseract.image_to_string(image, config="--psm 6")

def extract_document(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".pdf":
        page_texts = []
        text_pages = 0
        with pymupdf.open(filepath) as doc:
            for page in doc:
                text = page.get_text("text", sort=True)
                if len(text.strip()) > 30:
                    page_texts.append(text)
                    text_pages += 1
                else:
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False)
                    image = Image.open(BytesIO(pix.tobytes("png")))
                    page_texts.append(_ocr(image))
        method = "ekstraksi teks PDF" if text_pages == len(page_texts) else "ekstraksi PDF + OCR"
        return "\n\f\n".join(page_texts), method
    if ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        with Image.open(filepath) as image:
            return _ocr(image.copy()), "OCR gambar"
    raise ValueError("Format file tidak didukung.")
