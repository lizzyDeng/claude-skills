import struct
PNG_SIG=b"\x89PNG\r\n\x1a\n"
def is_png(path):
    with open(path,"rb") as f: return f.read(8)==PNG_SIG
def png_has_alpha(path):
    with open(path,"rb") as f:
        if f.read(8)!=PNG_SIG: return False
        ln=f.read(4)
        if len(ln)<4 or f.read(4)!=b"IHDR": return False
        ihdr=f.read(struct.unpack(">I",ln)[0]); f.read(4)
        if ihdr[9] in (4,6): return True
        while True:
            ln=f.read(4)
            if len(ln)<4: return False
            typ=f.read(4); size=struct.unpack(">I",ln)[0]
            if typ==b"tRNS": return True
            if typ in (b"IDAT",b"IEND"): return False
            f.seek(size+4,1)
