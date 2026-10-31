"""Deterministic synthetic PDF, including a scan-only page; no copyrighted source."""
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image, ImageDraw, ImageFont
root=Path(__file__).parent
out=root/'reading_fixture.pdf'
c=canvas.Canvas(str(out),pagesize=(595,842))
for page in range(1,3):
    c.bookmarkPage(f'page{page}');c.addOutlineEntry('Method' if page==1 else 'Limitations',f'page{page}')
    c.setFont('Helvetica',16);c.drawString(45,790,'Synthetic research reading fixture')
    c.setFont('Helvetica',12)
    text=c.beginText(45,755)
    for line in (['1 Method','Retrieval augmented generation uses retrieved documents.','Relevant passages support evidence-based answers.'] if page==1 else ['2 Limitations','A citation location does not establish semantic support.','Table 1: A = 2, B = 4. Equation: y = x + 1.']):text.textLine(line)
    c.drawText(text);c.drawString(45,40,f'Page {page}');c.showPage()
image=Image.new('RGB',(1200,500),'white');draw=ImageDraw.Draw(image)
font=ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf',42)
draw.text((35,65),'SCANNED PAGE: sample size 128.',font=font,fill='black')
draw.text((35,140),'Accuracy is 81 percent.',font=font,fill='black')
c.drawImage(ImageReader(image),45,450,width=500,height=210);c.showPage();c.save()
print(out)
