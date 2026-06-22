import struct, zlib
from pngutil import png_has_alpha, is_png
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct,trns=False):
    o=SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))
    if trns: o+=_chunk(b"tRNS",b"\x00")
    return o+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
def _w(t,b): p=t/"a.png"; p.write_bytes(b); return str(p)
def test_ct6(tmp_path): assert png_has_alpha(_w(tmp_path,_png(6))) is True
def test_ct4(tmp_path): assert png_has_alpha(_w(tmp_path,_png(4))) is True
def test_ct2_no(tmp_path): assert png_has_alpha(_w(tmp_path,_png(2))) is False
def test_ct3_trns(tmp_path): assert png_has_alpha(_w(tmp_path,_png(3,trns=True))) is True
def test_ct2_trns(tmp_path): assert png_has_alpha(_w(tmp_path,_png(2,trns=True))) is True
def test_not_png(tmp_path): assert png_has_alpha(_w(tmp_path,b"JFIF")) is False
def test_is_png(tmp_path): assert is_png(_w(tmp_path,_png(6))) and not is_png(_w(tmp_path,b"x"))
