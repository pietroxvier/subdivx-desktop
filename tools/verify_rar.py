"""Verificação local de RAR: cria um pacote de teste com o WinRAR instalado."""
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subdivx_app.core import SearchSpec, save_subtitles

rar = Path(r'C:\Program Files\WinRAR\Rar.exe')
if not rar.is_file():
    raise SystemExit('Teste requer Rar.exe para criar a fixture, não para usar o app.')
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    source = root / 'Example.S01E02.srt'
    source.write_text('1\n00:00:01,000 --> 00:00:03,000\nPrueba de extracción.\n', encoding='utf-8')
    package = root / 'example.rar'
    subprocess.run([str(rar), 'a', '-idq', '-ep', str(package), str(source)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    files = save_subtitles(package.read_bytes(), root / 'output',
                           SearchSpec('Example', 'Série', season=1, episode=2), 123, Event())
    assert len(files) == 1 and 'extracción' in files[0].read_text(encoding='utf-8')
    print('RAR roundtrip OK: archive creation, backend extraction, episode filter, UTF-8 output')
