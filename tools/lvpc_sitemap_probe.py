"""One-off: OCR two BuildLV monthly reports to see whether they list individual projects."""
import sys
sys.path.insert(0, "collectors")
from civicplus import PoliteSession
from pdf2image import convert_from_bytes
import pytesseract
cdn = PoliteSession("https://img1.wsimg.com")
B = "https://img1.wsimg.com/blobby/go/f0c03c33-493b-4de9-a633-323079cf8145/"
for u in [B + "BuildLV_sept_26.pdf", B + "downloads/8bd9779a-4dd0-478f-9169-8bbd1faa3fd8/January%202026%20BuildLV%20Report.pdf?ver=1793825667668"]:
    c = cdn.get(u).content
    pages = convert_from_bytes(c, dpi=200)
    print(f"== SAMPLE {u.split('/')[-1][:60]} ({len(pages)} pages)")
    for i, img in enumerate(pages[:3], 1):
        print(f"-- page {i} ({img.size})\n" + pytesseract.image_to_string(img)[:4000])
    print("== END SAMPLE")
