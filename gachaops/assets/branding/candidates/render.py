"""Original vector candidates; no changes to GachaOps or Linear."""
from pathlib import Path
import math
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QColor, QFont
from PySide6.QtSvg import QSvgRenderer

root = Path(__file__).parent
app = QGuiApplication([])
defs = '''<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#223455"/><stop offset="1" stop-color="#0c1529"/></linearGradient>
<linearGradient id="gold" x1="0" y1="0" x2="0.8" y2="1"><stop stop-color="#fff2bc"/><stop offset="0.42" stop-color="#f0c66a"/><stop offset="1" stop-color="#b77832"/></linearGradient>
<linearGradient id="edge" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#e3ad51"/><stop offset="0.6" stop-color="#805223"/><stop offset="1" stop-color="#d79b45"/></linearGradient>
</defs>'''
base = '<rect x="12" y="12" width="488" height="488" rx="108" fill="url(#bg)"/><rect x="13" y="13" width="486" height="486" rx="107" fill="none" stroke="#ffffff" stroke-opacity=".09" stroke-width="2"/>'

# A physical ring: a deep edge and a single bright upper face, with open center.
a = '''<g transform="rotate(-24 256 256)">
<ellipse cx="256" cy="286" rx="174" ry="105" fill="#000" opacity=".22"/>
<path fill="url(#edge)" fill-rule="evenodd" d="M80 254a176 112 0 1 0 352 0a176 112 0 1 0-352 0M123 246a133 74 0 1 1 266 0a133 74 0 1 1-266 0"/>
<path fill="url(#gold)" fill-rule="evenodd" d="M80 234a176 112 0 1 0 352 0a176 112 0 1 0-352 0M123 234a133 74 0 1 1 266 0a133 74 0 1 1-266 0"/>
<path d="M96 213C117 154 220 109 312 137" fill="none" stroke="#fff7da" stroke-width="6" stroke-linecap="round" opacity=".8"/>
</g>'''
b = '''<circle cx="256" cy="270" r="160" fill="none" stroke="#000" stroke-width="46" opacity=".18"/>
<circle cx="256" cy="250" r="155" fill="none" stroke="url(#gold)" stroke-width="46"/>
<path d="M151 147A147 147 0 0 1 292 109" fill="none" stroke="#fff5d4" stroke-width="5" stroke-linecap="round" opacity=".7"/>
<path d="M222 191Q222 184 229 188L315 242Q326 249 315 257L229 312Q222 316 222 309Z" fill="url(#gold)"/>
'''
c = '<circle cx="256" cy="256" r="143" fill="none" stroke="url(#gold)" stroke-width="28"/>'
for index in range(6):
    angle = math.radians(index * 60 - 90)
    x, y = 256 + 143 * math.cos(angle), 256 + 143 * math.sin(angle)
    c += f'<circle cx="{x:.2f}" cy="{y:.2f}" r="26" fill="url(#gold)" stroke="#14223b" stroke-width="9"/>'
c += '<path d="M256 186C268 234 278 244 326 256C278 268 268 278 256 326C244 278 234 268 186 256C234 244 244 234 256 186Z" fill="url(#gold)"/>'

images = []
for index, body in enumerate((a, b, c), 1):
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">{defs}{base}{body}</svg>'
    (root / f'candidate-{index}.svg').write_text(svg, encoding='utf-8')
    renderer = QSvgRenderer(svg.encode())
    assert renderer.isValid()
    sizes = {}
    for size in (16, 24, 32, 48, 64, 256, 512):
        image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image); painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter); painter.end()
        image.save(str(root / f'candidate-{index}-{size}.png'))
        sizes[size] = image
    images.append(sizes)

sheet = QImage(1260, 610, QImage.Format_ARGB32_Premultiplied); sheet.fill(QColor('#f4f6fa'))
p = QPainter(sheet); p.setRenderHint(QPainter.Antialiasing)
p.setFont(QFont('Microsoft YaHei UI', 18, QFont.DemiBold)); p.setPen(QColor('#17243b'))
p.drawText(QRectF(36, 20, 1188, 45), Qt.AlignLeft | Qt.AlignVCenter, 'GachaOps · 程序图标候选')
p.setFont(QFont('Microsoft YaHei UI', 10)); p.setPen(QColor('#64748b'))
p.drawText(QRectF(36, 65, 1188, 28), Qt.AlignLeft | Qt.AlignVCenter, 'One app to run them all')
labels = ['01  统御之环', '02  一环启动', '03  星环编排']
for index, sizes in enumerate(images):
    left = 28 + index * 414
    p.setPen(Qt.NoPen); p.setBrush(QColor('white')); p.drawRoundedRect(QRectF(left, 115, 390, 460), 20, 20)
    p.drawImage(QRectF(left + 60, 145, 270, 270), sizes[512])
    p.setFont(QFont('Microsoft YaHei UI', 13, QFont.DemiBold)); p.setPen(QColor('#17243b'))
    p.drawText(QRectF(left, 425, 390, 30), Qt.AlignCenter, labels[index])
    x = left + 80
    for size in (16, 24, 32, 48):
        p.drawImage(x, 493 + (48-size)//2, sizes[size])
        p.setFont(QFont('Microsoft YaHei UI', 8)); p.setPen(QColor('#64748b'))
        p.drawText(QRectF(x-5, 548, size+10, 17), Qt.AlignCenter, str(size))
        x += size + 34
p.end(); sheet.save(str(root / 'comparison.png'))
print(root / 'comparison.png')
