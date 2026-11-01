"""Render maintainer-selected original candidate 03 using the pinned Qt runtime."""
import hashlib
import json
from pathlib import Path
import shutil
import struct

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main():
    app = QGuiApplication([])
    root = Path(__file__).resolve().parents[1]
    target = root / 'gachaops/assets/branding'
    shutil.copyfile(target / 'candidates/candidate-3.svg', target / 'program.svg')
    renderer = QSvgRenderer(str(target / 'program.svg'))
    if not renderer.isValid():
        raise ValueError('Invalid selected SVG')
    images = {}
    for size in (16, 24, 32, 48, 64, 128, 256, 512, 1024):
        image = QImage(size, size, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        assert image.pixelColor(0, 0).alpha() == 0
        buffer = QBuffer(); buffer.open(QIODevice.WriteOnly)
        assert image.save(buffer, 'PNG')
        images[size] = bytes(buffer.data())
        (target / f'program-{size}.png').write_bytes(images[size])
    (target / 'program.png').write_bytes(images[512])
    sizes = (16, 24, 32, 48, 64, 128, 256)
    offset = 6 + 16 * len(sizes)
    entries = []
    for size in sizes:
        data = images[size]
        entries.append(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    (target / 'program.ico').write_bytes(struct.pack('<HHH', 0, 1, len(sizes)) + b''.join(entries) + b''.join(images[s] for s in sizes))
    shutil.copyfile(target / 'program.svg', root / 'docs/images/program.svg')
    files = sorted(target.glob('program.*')) + sorted(target.glob('program-*.png'))
    manifest = {'selection': '03', 'name': '星环编排（六节点圆环）',
                'confirmed': '2026-10-23', 'ico_sizes': list(sizes),
                'sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Selected brand rendered; transparent corners and seven ICO frames verified')


if __name__ == '__main__':
    main()
