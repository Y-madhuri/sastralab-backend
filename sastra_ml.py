"""
SASTRA LAB BOOKING SYSTEM v4 - WITH QR CODES + PUSH NOTIFICATIONS
===================================================================
NEW IN v4:
  ✅ QR CODE  → Every approved booking gets a scannable QR code
               (shown on screen + embedded in approval email)
               QR encodes: BookingID, Lab, Date, Time, Computers
               Admin can scan QR at lab entrance to verify booking
  ✅ BROWSER PUSH NOTIFICATIONS
               → "Allow notifications" prompt on login
               → Instant in-browser alert when booking is approved/rejected
               → Works even when user is on another browser tab
               → Uses Web Notifications API (no server needed)
               → Server-Sent Events (SSE) stream pushes real-time updates

HOW NOTIFICATIONS WORK:
  1. User logs in → browser asks "Allow notifications from this site"
  2. Flask opens /api/notify-stream (SSE endpoint) → long-lived connection
  3. When admin approves/rejects → server sends event on that stream
  4. Browser JS receives event → shows OS-level push notification
  5. Toast also shown inside the app

HOW QR CODES WORK:
  1. Booking submitted → QR generated instantly from booking data
  2. QR shown in "My Bookings" for approved bookings
  3. QR embedded in approval email (as inline SVG)
  4. Admin clicks "Verify QR" in admin panel → scans or pastes code
  5. QR contains JSON: {id, lab, date, time, computers, name}

SETUP:
  pip install flask flask-mail firebase-admin bcrypt xgboost scikit-learn numpy
  python sastra_ml_v4_final.py → http://localhost:5000

LOGINS (always work without Firebase):
  admin    / admin123
  faculty1 / faculty123
"""

from flask import Flask, request, jsonify, session, Response, stream_with_context
from flask_cors import CORS
from datetime import datetime, timedelta
import secrets, threading, time, hashlib, json, queue, base64, warnings
import numpy as np
from flask import Flask, request, jsonify, session, Response, stream_with_context, redirect
import os
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # only for localhost dev

# Suppress noisy warnings
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', category=FutureWarning)

# ── Optional imports ───────────────────────────────────────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_AVAILABLE = True
except: FIREBASE_AVAILABLE = False

try:
    import bcrypt
    BCRYPT_AVAILABLE = True
except: BCRYPT_AVAILABLE = False

try:
    import xgboost as xgb
    from sklearn.ensemble import IsolationForest, RandomForestClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import train_test_split
    ML_AVAILABLE = True
except Exception as e:
    print(f"⚠️  ML libs missing: {e}"); ML_AVAILABLE = False

try:
    from flask_mail import Mail, Message
    MAIL_AVAILABLE = True
except: MAIL_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except: PIL_AVAILABLE = False

try:
    import torch, torch.nn as nn, torch.nn.functional as F, math, pickle, re
    TORCH_AVAILABLE = True
except: TORCH_AVAILABLE = False

# ── Config ─────────────────────────────────────────────────────────────────────
# ── Email Configuration ───────────────────────────────────────────────────────
# To fix the 'BadCredentials' error:
#   1. Enable 2-Step Verification on your Google account
#   2. Go to https://myaccount.google.com/apppasswords
#   3. Create an App Password → choose "Mail" + "Windows Computer"
#   4. Copy the 16-character password and paste below
#   5. Use that password here (NOT your normal Gmail password)
EMAIL_HOST     = 'smtp.gmail.com'
EMAIL_PORT     = 587
EMAIL_USER     = os.environ.get('EMAIL_USER', '')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', '')
EMAIL_SENDER   = f'SASTRA Lab Booking <{EMAIL_USER}>'

FALLBACK_USERS = {
    'admin':    {'username':'admin',   'password_plain':'admin123',   'email':'admin@sastra.edu',   'name':'System Administrator','role':'admin',   'department':'Administration'},
    'faculty1': {'username':'faculty1','password_plain':'faculty123', 'email':'faculty@sastra.edu', 'name':'Dr. Faculty Member',  'role':'faculty', 'department':'ECE'},
}
# ── Google OAuth Config ────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')
GOOGLE_REDIRECT_URI  = os.environ.get('GOOGLE_REDIRECT_URI', 'http://localhost:5000/callback')
# ═══════════════════════════════════════════════════════════════════════════════
#  QR CODE GENERATOR — Fixed ISO/IEC 18004-compliant implementation
# ═══════════════════════════════════════════════════════════════════════════════

# ── Galois Field GF(256) arithmetic for Reed-Solomon ECC ──────────────────────
_GF_EXP = [0]*512; _GF_LOG = [0]*256
def _gf_init():
    x = 1
    for i in range(255):
        _GF_EXP[i] = x; _GF_LOG[x] = i
        x <<= 1
        if x & 0x100: x ^= 0x11d
    for i in range(255, 512): _GF_EXP[i] = _GF_EXP[i - 255]
_gf_init()

def _gf_mul(a, b):
    if a == 0 or b == 0: return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]

def _gf_poly_mul(p, q):
    r = [0] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        for j, b in enumerate(q):
            r[i + j] ^= _gf_mul(a, b)
    return r

def _rs_generator(n):
    g = [1]
    for i in range(n): g = _gf_poly_mul(g, [1, _GF_EXP[i]])
    return g

def _rs_remainder(data, gen):
    msg = list(data) + [0] * (len(gen) - 1)
    for i in range(len(data)):
        c = msg[i]
        if c:
            for j, g in enumerate(gen):
                msg[i + j] ^= _gf_mul(c, g)
    return msg[len(data):]

# ── QR Code Block & ECC specs (Version, ECC-M) ────────────────────────────────
# Each entry: (data_cw, ecc_cw_per_block, blocks_g1, data_cw_g1, blocks_g2, data_cw_g2)
_QR_SPEC = {
    1:  (16, 10, 1, 16, 0, 0),
    2:  (28, 16, 1, 28, 0, 0),
    3:  (44, 26, 1, 44, 0, 0),
    4:  (64, 18, 2, 32, 0, 0),
    5:  (86, 24, 2, 43, 0, 0),
    6:  (108,16, 4, 27, 0, 0),
    7:  (124,18, 4, 31, 0, 0),
    8:  (154,22, 2, 38, 2, 39),
    9:  (182,22, 3, 36, 2, 37),
    10: (216,26, 4, 43, 1, 44),
}

def _qr_encode(text: str):
    """
    QR Code encoder — Version 1-10, Byte mode, ECC Level M.
    Returns 2D boolean matrix or None on failure.
    All 5 bugs from the original fixed:
      1. Terminator properly padded
      2. Char-count bits correct (8 for v1-9)
      3. Format info uses correct BCH computation
      4. Zigzag skips timing column at col 6
      5. Mask applied only to data modules (not function patterns)
    """
    try:
        data = text.encode('iso-8859-1', errors='replace')
        n = len(data)
        caps = [16,28,44,64,86,108,124,154,182,216]
        version = next((i+1 for i,c in enumerate(caps) if c >= n), None)
        if version is None: return None

        spec = _QR_SPEC[version]
        data_cw = spec[0]; ecc_per_blk = spec[1]
        g1_blks = spec[2]; g1_sz = spec[3]
        g2_blks = spec[4]; g2_sz = spec[5]

        # ── Build bit stream ──────────────────────────────────
        bits = []
        def add_bits(val, length):
            for i in range(length-1,-1,-1): bits.append((val>>i)&1)

        add_bits(0b0100, 4)  # byte mode
        add_bits(n, 8 if version <= 9 else 16)  # char count (FIX #2)
        for byte in data: add_bits(byte, 8)

        # Terminator — properly pad up to 4 zeros (FIX #1)
        remaining = data_cw * 8 - len(bits)
        for _ in range(min(4, remaining)): bits.append(0)
        while len(bits) % 8: bits.append(0)
        pads = [0b11101100, 0b00010001]; pi = 0
        while len(bits) < data_cw * 8:
            add_bits(pads[pi % 2], 8); pi += 1

        # ── Split into blocks and compute ECC ─────────────────
        cw_bytes = [sum(bits[i*8+j] << (7-j) for j in range(8)) for i in range(data_cw)]
        blocks = []; pos = 0
        for _ in range(g1_blks):
            blocks.append(cw_bytes[pos:pos+g1_sz]); pos += g1_sz
        for _ in range(g2_blks):
            blocks.append(cw_bytes[pos:pos+g2_sz]); pos += g2_sz

        gen = _rs_generator(ecc_per_blk)
        ecc_blocks = [_rs_remainder(blk, gen) for blk in blocks]

        # ── Interleave data then ECC ───────────────────────────
        interleaved = []
        max_blk = max(len(b) for b in blocks)
        for i in range(max_blk):
            for blk in blocks:
                if i < len(blk): interleaved.append(blk[i])
        for i in range(ecc_per_blk):
            for ecc in ecc_blocks: interleaved.append(ecc[i])

        # ── Build QR matrix ───────────────────────────────────
        sz = 17 + 4 * version
        mat = [[None]*sz for _ in range(sz)]  # None = unset (data module)

        def set_fn(r, c, v):   # function pattern: always sets, marks as non-data
            if 0 <= r < sz and 0 <= c < sz: mat[r][c] = bool(v)

        def is_fn(r, c):
            return mat[r][c] is not None and not isinstance(mat[r][c], bool) or \
                   (mat[r][c] is not None)

        # Finder patterns + separators
        def finder(ro, co):
            for dr in range(7):
                for dc in range(7):
                    v = (dr in (0,6) or dc in (0,6) or (2<=dr<=4 and 2<=dc<=4))
                    set_fn(ro+dr, co+dc, v)
            # separator (light border)
            for i in range(8):
                if 0<=ro-1<sz and 0<=co+i<sz: set_fn(ro-1, co+i, 0)
                if 0<=ro+7<sz and 0<=co+i<sz: set_fn(ro+7, co+i, 0)
                if 0<=ro+i<sz and 0<=co-1<sz: set_fn(ro+i, co-1, 0)
                if 0<=ro+i<sz and 0<=co+7<sz: set_fn(ro+i, co+7, 0)

        finder(0, 0); finder(0, sz-7); finder(sz-7, 0)

        # Timing patterns
        for i in range(8, sz-8):
            set_fn(6, i, i%2==0); set_fn(i, 6, i%2==0)

        # Format info regions (set to 0 first, will overwrite later)
        fmt_positions = (
            [(8,i) for i in range(9) if i != 6] +
            [(i,8) for i in range(8,-1,-1) if i != 6]
        )
        fmt_positions2 = [(8, sz-8+i) for i in range(8)] + [(sz-7+i, 8) for i in range(7)]
        for r,c in fmt_positions + fmt_positions2: set_fn(r, c, 0)

        # Dark module
        set_fn(4*version+9, 8, 1)

        # Alignment patterns (v2+)
        _align = {1:[],2:[6,18],3:[6,22],4:[6,26],5:[6,30],
                  6:[6,34],7:[6,22,38],8:[6,24,42],9:[6,26,46],10:[6,28,50]}
        centers = _align.get(version, [])
        for r in centers:
            for c in centers:
                if mat[r][c] is not None: continue  # overlaps finder
                for dr in range(-2,3):
                    for dc in range(-2,3):
                        v = (dr in(-2,2) or dc in(-2,2) or (dr==0 and dc==0))
                        set_fn(r+dr, c+dc, v)

        # ── Place data bits in zigzag (FIX #4 + #5) ──────────
        all_bits = []
        for byte in interleaved:
            for i in range(7,-1,-1): all_bits.append((byte>>i)&1)
        # remainder bits
        remainder = [0,7,7,7,7,7,0,0,0,0][version-1]
        all_bits += [0]*remainder

        bit_idx = 0
        col = sz-1; going_up = True
        while col >= 1:
            if col == 6: col -= 1  # skip timing column (FIX #4)
            rows = range(sz-1, -1, -1) if going_up else range(sz)
            for row in rows:
                for dc in range(2):
                    c = col - dc
                    if 0 <= c < sz and mat[row][c] is None:
                        b = all_bits[bit_idx] if bit_idx < len(all_bits) else 0
                        bit_idx += 1
                        # Apply mask pattern 2: (row*col)%3==0 but ONLY to data (FIX #5)
                        mat[row][c] = bool(b ^ ((row * c) % 3 == 0))
            going_up = not going_up
            col -= 2

        # ── Write format information with BCH (FIX #3) ────────
        # ECC Level M = 0b01, mask pattern 2 = 0b010 → data = 0b01_010 = 0b01010
        fmt_data = 0b01010
        # BCH with generator 0x537
        fmt_poly = fmt_data << 10
        for i in range(4, -1, -1):
            if fmt_poly & (1 << (i + 10)):
                fmt_poly ^= (0x537 << i)
        fmt_word = ((fmt_data << 10) | fmt_poly) ^ 0b101010000010010  # XOR mask

        fmt_bits = [(fmt_word >> i) & 1 for i in range(14, -1, -1)]
        # Position 1 (around top-left finder)
        fp1 = [(8,0),(8,1),(8,2),(8,3),(8,4),(8,5),(8,7),(8,8),
               (7,8),(5,8),(4,8),(3,8),(2,8),(1,8),(0,8)]
        for i,(r,c) in enumerate(fp1): mat[r][c] = bool(fmt_bits[i])
        # Position 2 (top-right and bottom-left)
        fp2 = [(sz-1,8),(sz-2,8),(sz-3,8),(sz-4,8),(sz-5,8),(sz-6,8),(sz-7,8),
               (8,sz-8),(8,sz-7),(8,sz-6),(8,sz-5),(8,sz-4),(8,sz-3),(8,sz-2),(8,sz-1)]
        for i,(r,c) in enumerate(fp2): mat[r][c] = bool(fmt_bits[i])

        return mat
    except Exception as e:
        return None


def make_qr_svg(data: str, cell=6, quiet=2) -> str:
    """Generate QR SVG from encoded matrix."""
    try:
        mat = _qr_encode(data)
        if mat is None: return _qr_fallback_svg(data)
        sz = len(mat); total = (sz + quiet*2) * cell
        rects = []
        for r in range(sz):
            for c in range(sz):
                if mat[r][c]:
                    x = (c+quiet)*cell; y = (r+quiet)*cell
                    rects.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" fill="black"/>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="{total}" '
                f'style="background:white">'
                f'{"".join(rects)}</svg>')
    except Exception:
        return _qr_fallback_svg(data)


def make_qr_png_b64(data: str, cell=8, quiet=4) -> str:
    """Generate QR PNG as base64 data URI using PIL."""
    try:
        if not PIL_AVAILABLE: raise ImportError
        mat = _qr_encode(data)
        if mat is None: raise ValueError
        sz = len(mat); total = (sz + quiet*2) * cell
        img = Image.new('1', (total, total), 1)
        pixels = img.load()
        for r in range(sz):
            for c in range(sz):
                if mat[r][c]:
                    for dy in range(cell):
                        for dx in range(cell):
                            pixels[(c+quiet)*cell+dx, (r+quiet)*cell+dy] = 0
        import io
        buf = io.BytesIO(); img.save(buf, format='PNG')
        enc = base64.b64encode(buf.getvalue()).decode()
        return f'data:image/png;base64,{enc}'
    except Exception:
        svg = make_qr_svg(data)
        enc = base64.b64encode(svg.encode()).decode()
        return f'data:image/svg+xml;base64,{enc}'


def _qr_fallback_svg(data: str) -> str:
    lines = data[:80].split('|')
    h = 120 + len(lines) * 22
    rows = ''.join(f'<text x="10" y="{60+i*22}" font-size="13" fill="#333">{l}</text>' for i,l in enumerate(lines))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="260" height="{h}" '
            f'style="background:white;border-radius:8px;border:2px solid #667eea">'
            f'<rect width="260" height="40" fill="#667eea"/>'
            f'<text x="10" y="26" font-size="15" font-weight="bold" fill="white">🎓 SASTRA Booking</text>'
            f'{rows}</svg>')


# ═══════════════════════════════════════════════════════════════════════════════
#  LLM CHATBOT — Integrated from train_lab_llm.py
# ═══════════════════════════════════════════════════════════════════════════════

LLM_AVAILABLE = False
_llm_model    = None
_llm_vocab    = None

print(f"🔧  PyTorch available: {TORCH_AVAILABLE}")

if TORCH_AVAILABLE:
    import math as _math, re as _re, pickle as _pickle, os as _os

    # ── Vocabulary ────────────────────────────────────────────────────────────
    class _Vocabulary:
        SPECIALS = ['<PAD>','<UNK>','<BOS>','<EOS>','<SEP>']
        def __init__(self):
            self.w2i = {}; self.i2w = {}
            for t in self.SPECIALS: self._add(t)
        def _add(self, w):
            if w not in self.w2i:
                i = len(self.w2i); self.w2i[w] = i; self.i2w[i] = w
        @staticmethod
        def tokenize(text):
            text = str(text).lower().strip()
            text = _re.sub(r'(\d+:\d+-\d+:\d+)', r' \1 ', text)
            text = _re.sub(r'([?.!,;/()\'"&#])', r' \1 ', text)
            return [t for t in text.split() if t]
        def encode(self, text):
            unk = self.w2i['<UNK>']
            return [self.w2i.get(w, unk) for w in self.tokenize(text)]
        def decode(self, ids):
            specials = set(self.SPECIALS); words = []
            for i in ids:
                w = self.i2w.get(i, '<UNK>')
                if w == '<EOS>': break
                if w not in specials: words.append(w)
            text = ' '.join(words)
            text = _re.sub(r' ([?.!,;:])', r'\1', text)
            text = _re.sub(r'\( ', '(', text)
            text = _re.sub(r' \)', ')', text)
            return text
        @property
        def pad_id(self): return self.w2i['<PAD>']
        @property
        def bos_id(self): return self.w2i['<BOS>']
        @property
        def eos_id(self): return self.w2i['<EOS>']
        @property
        def sep_id(self): return self.w2i['<SEP>']

    # ── Model architecture ────────────────────────────────────────────────────
    class _SinusoidalPE(torch.nn.Module):
        def __init__(self, d, max_len, drop):
            super().__init__()
            self.drop = torch.nn.Dropout(drop)
            pe  = torch.zeros(max_len, d)
            pos = torch.arange(max_len).unsqueeze(1).float()
            div = torch.exp(torch.arange(0, d, 2).float() * (-_math.log(10000.0) / d))
            pe[:, 0::2] = torch.sin(pos * div)
            pe[:, 1::2] = torch.cos(pos * div)
            self.register_buffer('pe', pe.unsqueeze(0))
        def forward(self, x):
            return self.drop(x + self.pe[:, :x.size(1)])

    class _CausalAttn(torch.nn.Module):
        def __init__(self, d, h, drop):
            super().__init__()
            self.h = h; self.dh = d // h
            self.qkv  = torch.nn.Linear(d, 3*d, bias=False)
            self.proj = torch.nn.Linear(d, d,   bias=False)
            self.da   = torch.nn.Dropout(drop)
            self.dr   = torch.nn.Dropout(drop)
        def forward(self, x, cm, pm):
            B, T, C = x.shape
            q, k, v = self.qkv(x).chunk(3, dim=-1)
            def split(t): return t.view(B, T, self.h, self.dh).transpose(1, 2)
            q, k, v = split(q), split(k), split(v)
            sc = torch.matmul(q, k.transpose(-2, -1)) / _math.sqrt(self.dh) + cm
            sc = sc.masked_fill(pm.unsqueeze(1).unsqueeze(2), float('-inf'))
            w  = self.da(torch.nn.functional.softmax(sc, dim=-1))
            o  = torch.matmul(w, v).transpose(1, 2).contiguous().view(B, T, C)
            return self.dr(self.proj(o))

    class _FFN(torch.nn.Module):
        def __init__(self, d, ff, drop):
            super().__init__()
            self.net = torch.nn.Sequential(
                torch.nn.Linear(d, ff), torch.nn.GELU(), torch.nn.Dropout(drop),
                torch.nn.Linear(ff, d), torch.nn.Dropout(drop))
        def forward(self, x): return self.net(x)

    class _Block(torch.nn.Module):
        def __init__(self, d, h, ff, drop):
            super().__init__()
            self.ln1  = torch.nn.LayerNorm(d); self.attn = _CausalAttn(d, h, drop)
            self.ln2  = torch.nn.LayerNorm(d); self.ffn  = _FFN(d, ff, drop)
        def forward(self, x, cm, pm):
            x = x + self.attn(self.ln1(x), cm, pm)
            x = x + self.ffn(self.ln2(x))
            return x

    class _LabLLM(torch.nn.Module):
        D=512; H=8; L=6; FF=2048; DR=0.1; ML=128
        def __init__(self, vocab_size, pad_id):
            super().__init__()
            self.pad_id  = pad_id
            d = self.D
            self.tok_emb = torch.nn.Embedding(vocab_size, d, padding_idx=pad_id)
            self.pos_enc = _SinusoidalPE(d, self.ML, self.DR)
            self.blocks  = torch.nn.ModuleList([_Block(d, self.H, self.FF, self.DR) for _ in range(self.L)])
            self.ln_f    = torch.nn.LayerNorm(d)
            self.lm_head = torch.nn.Linear(d, vocab_size, bias=False)
            self.lm_head.weight = self.tok_emb.weight
        @staticmethod
        def _causal_mask(T, device):
            return torch.triu(torch.full((T, T), float('-inf'), device=device), diagonal=1)
        def forward(self, ids):
            B, T   = ids.shape
            device = ids.device
            pm = (ids == self.pad_id)
            cm = self._causal_mask(T, device)
            x  = self.pos_enc(self.tok_emb(ids))
            for blk in self.blocks: x = blk(x, cm, pm)
            return self.lm_head(self.ln_f(x))

    # ── Load checkpoints ──────────────────────────────────────────────────────
    def _load_llm():
        global _llm_model, _llm_vocab, LLM_AVAILABLE
        _here = _os.path.dirname(_os.path.abspath(__file__))
        _candidates = [
            r'D:\Major project\llm\checkpoints',
            _os.path.join(_here, 'checkpoints'),
            _os.path.join(_os.getcwd(), 'checkpoints'),
        ]
        vocab_path = model_path = None
        for _dir in _candidates:
            _v = _os.path.join(_dir, 'vocab.pkl')
            _m = _os.path.join(_dir, 'best.pt')
            print(f'🔍 Checking: {_dir}')
            print(f'     vocab.pkl : {"✅" if _os.path.exists(_v) else "❌"}')
            print(f'     best.pt   : {"✅" if _os.path.exists(_m) else "❌"}')
            if _os.path.exists(_v) and _os.path.exists(_m):
                vocab_path, model_path = _v, _m
                print(f'✅  Using: {_dir}')
                break
        if not vocab_path:
            print('⚠️   No checkpoints found — chatbot disabled.')
            return
        try:
            print('📦  Loading vocab…')
            # Pickle needs the original class name 'Vocabulary' to deserialize.
            # Register our _Vocabulary class under every name pickle might look for.
            import sys as _sys
            _sys.modules[__name__].Vocabulary = _Vocabulary
            _sys.modules['__main__'].Vocabulary = _Vocabulary
            # Also handle if it was saved from train_lab_llm module
            import types as _types
            _fake = _types.ModuleType('train_lab_llm')
            _fake.Vocabulary = _Vocabulary
            _sys.modules['train_lab_llm'] = _fake

            with open(vocab_path, 'rb') as f: raw = _pickle.load(f)
            vocab = _Vocabulary()
            if hasattr(raw, 'w2i'):
                vocab.w2i = raw.w2i; vocab.i2w = raw.i2w
            elif isinstance(raw, dict):
                vocab.w2i = raw.get('w2i', raw)
                vocab.i2w = {v: k for k, v in vocab.w2i.items()}
            else:
                print(f'⚠️   Unexpected vocab format: {type(raw)}'); return
            print(f'📦  Vocab size: {len(vocab.w2i)} tokens')
            device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
            print(f'📦  Loading model on {device}…')
            model = _LabLLM(len(vocab.w2i), vocab.pad_id).to(device)
            ck    = torch.load(model_path, map_location=device)
            state = ck.get('model_state_dict', ck)
            model.load_state_dict(state, strict=False)
            model.eval()
            _llm_model = model; _llm_vocab = vocab; LLM_AVAILABLE = True
            print('🤖  LLM chatbot loaded successfully!')
        except Exception as e:
            import traceback
            print(f'⚠️   LLM load error: {e}')
            traceback.print_exc()

    _load_llm()
def llm_generate(question: str) -> str:
    if not LLM_AVAILABLE or _llm_model is None or _llm_vocab is None:
        return "LLM chatbot not available. Please train the model first using train_lab_llm.py."
    try:
        device = next(_llm_model.parameters()).device
        q_ids = _llm_vocab.encode(question)
        ids = [_llm_vocab.bos_id] + q_ids + [_llm_vocab.sep_id]
        ids_t = torch.tensor([ids], dtype=torch.long, device=device)
        generated = []
        with torch.no_grad():
            for _ in range(80):
                if ids_t.shape[1] > 128: ids_t = ids_t[:, -128:]
                logits = _llm_model(ids_t)
                next_logits = logits[0, -1, :] / 0.7
                vals, idxs = torch.topk(next_logits, 40)
                probs = F.softmax(vals, dim=-1)
                chosen = idxs[torch.multinomial(probs, 1)].item()
                if chosen == _llm_vocab.eos_id: break
                generated.append(chosen)
                ids_t = torch.cat([ids_t, torch.tensor([[chosen]], device=device)], dim=1)
        answer = _llm_vocab.decode(generated)
        return answer if answer.strip() else "I couldn\'t generate a relevant answer. Please try rephrasing."
    except Exception as e:
        return f"Error generating response: {e}"



def booking_to_qr_text(b: dict) -> str:
    """Full human-readable booking info encoded in QR — readable by any scanner."""
    bid   = str(b.get('id',''))
    lab   = LABS.get(b.get('lab',''), {}).get('name', b.get('lab',''))
    date  = b.get('date','')
    slot  = b.get('time','')
    pcs   = ', '.join(map(str, sorted(b.get('computers',[]))))
    name  = b.get('userName','')
    email = b.get('userEmail','')
    dept  = b.get('userDept', b.get('department',''))
    purp  = b.get('purpose','')[:80]
    status= b.get('status','').upper()
    return (
        f"=== SASTRA LAB BOOKING ===\n"
        f"Booking ID : {bid}\n"
        f"Student    : {name}\n"
        f"Email      : {email}\n"
        f"Lab        : {lab}\n"
        f"Date       : {date}\n"
        f"Time Slot  : {slot}\n"
        f"Computers  : {pcs}\n"
        f"Purpose    : {purp}\n"
        f"Status     : {status}\n"
        f"========================="
    )


def make_qr_image_b64(b: dict) -> str:
    """
    Generate a scannable QR PNG using the 'qrcode' library (preferred)
    or fall back to our pure-Python encoder.
    Returns a base64 data URI string.
    """
    text = booking_to_qr_text(b)
    # ── Try qrcode library first (pip install qrcode[pil]) ────────────────
    try:
        import qrcode as _qrlib
        from qrcode.image.pil import PilImage
        import io
        qr = _qrlib.QRCode(
            version=None,
            error_correction=_qrlib.constants.ERROR_CORRECT_M,
            box_size=8,
            border=4,
        )
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        enc = base64.b64encode(buf.getvalue()).decode()
        return f'data:image/png;base64,{enc}'
    except ImportError:
        pass
    except Exception as e:
        print(f'qrcode lib error: {e}')

    # ── Fallback: our pure-Python encoder via PIL ─────────────────────────
    try:
        return make_qr_png_b64(text)
    except Exception:
        pass

    # ── Last resort: SVG ──────────────────────────────────────────────────
    svg = make_qr_svg(text)
    enc = base64.b64encode(svg.encode()).decode()
    return f'data:image/svg+xml;base64,{enc}'


def qr_data_uri(b: dict) -> str:
    """Returns base64 data URI of QR for embedding in HTML/email."""
    return make_qr_image_b64(b)


# ═══════════════════════════════════════════════════════════════════════════════
#  SERVER-SENT EVENTS (Push Notifications)
# ═══════════════════════════════════════════════════════════════════════════════

# Map: user_id → list of Queue objects (one per open browser tab)
_sse_clients: dict[str, list] = {}
_sse_lock = threading.Lock()

def sse_push(user_id: str, event_type: str, data: dict):
    """Push a notification to all open SSE streams for a user."""
    with _sse_lock:
        qs = _sse_clients.get(user_id, [])
        dead = []
        for q in qs:
            try: q.put_nowait({'type': event_type, 'data': data})
            except: dead.append(q)
        for q in dead: qs.remove(q)

def sse_push_all(event_type: str, data: dict):
    """Push to every connected user."""
    with _sse_lock:
        for uid in list(_sse_clients.keys()):
            sse_push(uid, event_type, data)

# ═══════════════════════════════════════════════════════════════════════════════
#  ML ENGINE (unchanged from v3)
# ═══════════════════════════════════════════════════════════════════════════════

class MLEngine:
    SLOT_MAP   = {'8:45am-9:45am':0,'9:45am-10:45am':1,'11:00am-12:00noon':2,
                  '12:00noon-1:00pm':3,'1:00pm-2:00pm':4,'2:00pm-3:00pm':5,
                  '3:15pm-4:15pm':6,'4:15pm-5:15pm':7}
    LAB_MAP    = {'dsp1':0,'dsp2':1,'ai':2}
    ROLE_MAP   = {'student':0,'faculty':1,'admin':2}
    PEAK_SLOTS = {2,3,5}

    def __init__(self):
        self.xgb_model=self.iso_model=self.rf_model=self.scaler=None
        self.trained=False; self.training_stats={}; self.last_retrain=None

    def _features(self, lab, date, time_slot, num_computers, purpose,
                  user_role='student', noshow_rate=0.0, total_bookings=0):
        try:
            d=datetime.strptime(date,'%Y-%m-%d'); dow=d.weekday(); is_weekend=1 if dow>=5 else 0
        except: dow=0; is_weekend=0
        slot_idx=self.SLOT_MAP.get(time_slot,4); is_peak=1 if slot_idx in self.PEAK_SLOTS else 0
        lab_id=self.LAB_MAP.get(lab,0); role_id=self.ROLE_MAP.get(user_role,0)
        purp_len=min(len(purpose or ''),500); comp_ratio=num_computers/33.0; hour=datetime.now().hour
        ns_penalty=min(noshow_rate,1.0); exp_score=min(total_bookings/20.0,1.0)
        return np.array([[lab_id,dow,slot_idx,num_computers,is_peak,is_weekend,
                          role_id,purp_len,hour,comp_ratio,ns_penalty,exp_score]],dtype=float)

    def _synthetic(self, n=3000):
        rng=np.random.default_rng(42); rows,ya,yd=[],[],[]
        for _ in range(n):
            lab=int(rng.integers(0,3)); day=int(rng.integers(0,7)); slot=int(rng.integers(0,8))
            comps=int(rng.integers(1,34)); role=int(rng.integers(0,2)); hour=int(rng.integers(7,22))
            purp=int(rng.integers(0,501)); we=1 if day>=5 else 0; peak=1 if slot in self.PEAK_SLOTS else 0
            cr=comps/33.0; ns=float(rng.random()*0.4); exp=float(rng.random())
            prob=0.65
            if role==1: prob+=0.20
            if we: prob-=0.30
            if peak and comps>20: prob-=0.25
            if purp<10: prob-=0.15
            if purp>50: prob+=0.10
            if comps<=5: prob+=0.05
            prob-=ns*0.30; prob+=exp*0.10; prob=float(np.clip(prob,0.05,0.95))
            rows.append([lab,day,slot,comps,peak,we,role,purp,hour,cr,ns,exp])
            ya.append(int(rng.random()<prob))
            yd.append(2 if(peak and not we and comps>15) else(1 if peak else 0))
        return np.array(rows,dtype=float),np.array(ya),np.array(yd)

    def train(self, db_ref=None, force_synthetic=False):
        if not ML_AVAILABLE: return False
        print("🤖 Training ML models …")
        Xs,yas,yds=self._synthetic(3000)
        Xa,ya,yd=Xs,yas,yds; src="synthetic (3000)"
        self.scaler=StandardScaler(); Xsc=self.scaler.fit_transform(Xa)
        Xtr,Xte,ytr,yte=train_test_split(Xsc,ya,test_size=0.2,random_state=42)
        self.xgb_model=xgb.XGBClassifier(n_estimators=200,max_depth=6,learning_rate=0.07,
            subsample=0.85,colsample_bytree=0.85,eval_metric='logloss',random_state=42,verbosity=0)
        self.xgb_model.fit(Xtr,ytr); xacc=self.xgb_model.score(Xte,yte)
        print(f"   ✅ XGBoost {xacc:.2%}")
        self.iso_model=IsolationForest(n_estimators=200,contamination=0.07,random_state=42)
        self.iso_model.fit(Xsc); print("   ✅ IsolationForest ready")
        Xtr2,Xte2,ytr2,yte2=train_test_split(Xsc,yd,test_size=0.2,random_state=42)
        self.rf_model=RandomForestClassifier(n_estimators=150,max_depth=7,random_state=42,n_jobs=-1)
        self.rf_model.fit(Xtr2,ytr2); racc=self.rf_model.score(Xte2,yte2)
        print(f"   ✅ RandomForest {racc:.2%}")
        self.trained=True; self.last_retrain=datetime.now()
        self.training_stats={'xgb_accuracy':round(xacc,4),'rf_accuracy':round(racc,4),
                             'data_source':src,'trained_at':datetime.now().isoformat(),'features':12}
        print("🎉 ML ready!\n"); return True

    def predict(self, lab, date, time_slot, num_computers, purpose,
                user_role='student', noshow_rate=0.0, total_bookings=0):
        if not self.trained or not ML_AVAILABLE: return self._fallback(num_computers,user_role)
        try:
            X=self.scaler.transform(self._features(lab,date,time_slot,num_computers,purpose,user_role,noshow_rate,total_bookings))
            prob=float(self.xgb_model.predict_proba(X)[0][1]); adj=prob*(1-noshow_rate*0.3)
            iso=float(self.iso_model.decision_function(X)[0]); anom=int(self.iso_model.predict(X)[0])==-1
            anompct=round(max(0,min(1,0.5-iso)),3); dem=int(self.rf_model.predict(X)[0])
            dlbl=['Low','Medium','High'][dem]; dprob=self.rf_model.predict_proba(X)[0].tolist()
            if anom:   st='pending'; reason=f"⚠️ Anomaly ({anompct:.0%}). Admin review."; rec='manual_review'
            elif adj>=0.50: st='approved'; reason=f"✅ {adj:.0%} conf. Demand: {dlbl}."; rec='auto_approve'
            else:      st='pending'; reason=f"⏳ Low conf ({adj:.0%}). Admin review."; rec='manual_review'
            return {'ml_status':st,'ml_reason':reason,'recommendation':rec,
                    'xgb_confidence':round(adj,4),'raw_confidence':round(prob,4),
                    'anomaly_score':anompct,'is_anomaly':anom,'demand_level':dlbl,
                    'demand_proba':[round(p,3) for p in dprob],'models_used':['XGBoost','IsolationForest','RandomForest']}
        except Exception as e: print(f"ML error: {e}"); return self._fallback(num_computers,user_role)

    def _fallback(self, nc, role):
        ok=role=='faculty' or nc<=10
        return {'ml_status':'approved' if ok else 'pending','ml_reason':'Rule-based fallback',
                'recommendation':'auto_approve' if ok else 'manual_review',
                'xgb_confidence':0.70 if ok else 0.45,'raw_confidence':0.70 if ok else 0.45,
                'anomaly_score':0.0,'is_anomaly':False,'demand_level':'Medium',
                'demand_proba':[0.33,0.34,0.33],'models_used':['RuleEngine']}

    def demand_forecast(self, lab, date):
        if not self.trained: return {}
        slots=['8:45am-9:45am','9:45am-10:45am','11:00am-12:00noon','12:00noon-1:00pm',
               '1:00pm-2:00pm','2:00pm-3:00pm','3:15pm-4:15pm','4:15pm-5:15pm']
        return {s:self.predict(lab,date,s,10,'lab session','student')['demand_level'] for s in slots}

    def get_user_stats(self, db_ref, user_id):
        if not db_ref: return 0.0, 0
        try:
            total=0; noshow=0
            for doc in db_ref.collection('bookings').where('userId','==',user_id).stream():
                b=doc.to_dict(); total+=1
                if b.get('noShow'): noshow+=1
            return (noshow/total if total>0 else 0.0), total
        except: return 0.0, 0


ml_engine = MLEngine()

# ═══════════════════════════════════════════════════════════════════════════════
#  FLASK APP
# ═══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))

# ── CORS — required so the browser (Lovable/Railway frontend) can call this API
ALLOWED_ORIGINS = [
    "http://localhost:5173",          # Vite dev
    "http://localhost:3000",          # CRA dev
    "http://localhost:4173",          # Vite preview
]
# Add any extra origins from environment (e.g. your Lovable URL)
_extra = os.environ.get('CORS_ORIGINS', '')
if _extra:
    ALLOWED_ORIGINS += [o.strip() for o in _extra.split(',') if o.strip()]

CORS(app,
     supports_credentials=True,
     origins=ALLOWED_ORIGINS,
     allow_headers=["Content-Type", "Authorization"],
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])

# Session cookie settings (required for cross-origin cookies)
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,       # set to False only for plain HTTP dev
    SESSION_COOKIE_HTTPONLY=True,
    MAIL_SERVER=EMAIL_HOST,
    MAIL_PORT=EMAIL_PORT,
    MAIL_USE_TLS=True,
    MAIL_USERNAME=EMAIL_USER,
    MAIL_PASSWORD=EMAIL_PASSWORD,
    MAIL_DEFAULT_SENDER=EMAIL_SENDER,
)
mail = Mail(app) if MAIL_AVAILABLE else None

db = None
if FIREBASE_AVAILABLE:
    try:
        if not firebase_admin._apps:
            cred=credentials.Certificate('serviceAccountKey.json')
            firebase_admin.initialize_app(cred)
        db=firestore.client(); print("✅ Firebase ready")
    except Exception as e: print(f"⚠️ Firebase: {e}")

LABS = {
    'dsp1':{'name':'DSP Lab I',        'floor':'1st Floor','computers':33,'icon':'🖥️'},
    'dsp2':{'name':'DSP Lab II',       'floor':'1st Floor','computers':33,'icon':'💻'},
    'ai':  {'name':'AI & Robotics Lab','floor':'2nd Floor','computers':33,'icon':'🤖'}
}
TIME_SLOTS=['8:45am-9:45am','9:45am-10:45am','11:00am-12:00noon','12:00noon-1:00pm',
            '1:00pm-2:00pm','2:00pm-3:00pm','3:15pm-4:15pm','4:15pm-5:15pm']
SLOT_START_TIMES={'8:45am-9:45am':'08:45','9:45am-10:45am':'09:45',
                  '11:00am-12:00noon':'11:00','12:00noon-1:00pm':'12:00',
                  '1:00pm-2:00pm':'13:00','2:00pm-3:00pm':'14:00',
                  '3:15pm-4:15pm':'15:15','4:15pm-5:15pm':'16:15'}

TIMETABLE = {
    'dsp1': {
        'Monday':    {'11:00am-12:00noon':{'course':'ECE317','name':'FPGA Laboratory','section':'A2','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'12:00noon-1:00pm':{'course':'ECE317','name':'FPGA Laboratory','section':'A2','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'}},
        'Tuesday':   {'11:00am-12:00noon':{'course':'ECE317','name':'FPGA Laboratory','section':'B2','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'12:00noon-1:00pm':{'course':'ECE317','name':'FPGA Laboratory','section':'B2','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'1:00pm-2:00pm':{'course':'ECE317','name':'FPGA Laboratory','section':'C2','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'2:00pm-3:00pm':{'course':'ECE317','name':'FPGA Laboratory','section':'C2','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'3:15pm-4:15pm':{'course':'ECE317','name':'FPGA Laboratory','section':'D2','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'}},
        'Wednesday': {'8:45am-9:45am':{'course':'ECE317','name':'FPGA Laboratory','section':'D1','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'9:45am-10:45am':{'course':'ECE317','name':'FPGA Laboratory','section':'D1','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'11:00am-12:00noon':{'course':'ECE317','name':'FPGA Laboratory','section':'E2','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'12:00noon-1:00pm':{'course':'ECE317','name':'FPGA Laboratory','section':'E2','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'3:15pm-4:15pm':{'course':'ECE317','name':'FPGA Laboratory','section':'A1','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'4:15pm-5:15pm':{'course':'ECE317','name':'FPGA Laboratory','section':'A1','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'}},
        'Thursday':  {'8:45am-9:45am':{'course':'EIE','name':'II Year EIE','section':'','year':'II Year','faculty':'Various'},'9:45am-10:45am':{'course':'EIE','name':'II Year EIE','section':'','year':'II Year','faculty':'Various'},'1:00pm-2:00pm':{'course':'ECE317','name':'FPGA Laboratory','section':'E1','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'2:00pm-3:00pm':{'course':'ECE317','name':'FPGA Laboratory','section':'E1','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'3:15pm-4:15pm':{'course':'ECE335','name':'FPGA Architecture','section':'','year':'III/ECE','faculty':'Dr.N. Mohanraj'},'4:15pm-5:15pm':{'course':'ECE335','name':'FPGA Architecture','section':'','year':'III/ECE','faculty':'Dr.N. Mohanraj'}},
        'Friday':    {'8:45am-9:45am':{'course':'ECE335','name':'FPGA Architecture','section':'','year':'III/ECE','faculty':'Dr.N. Mohanraj'},'9:45am-10:45am':{'course':'ECE335','name':'FPGA Architecture','section':'','year':'III/ECE','faculty':'Dr.N. Mohanraj'},'11:00am-12:00noon':{'course':'ECE317','name':'FPGA Laboratory','section':'C1','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'},'12:00noon-1:00pm':{'course':'ECE317','name':'FPGA Laboratory','section':'C1','year':'III(A-E)','faculty':'Dr.S. Archana, Dr.A. Sridevi'}},
        'Saturday':{},'Sunday':{}
    },
    'dsp2': {
        'Monday':    {'8:45am-9:45am':{'course':'ECE342','name':'Applied Model Based Design','section':'','year':'III/EEE','faculty':'Dr. A. Sridevi, Dr. Jeyanthi J'},'9:45am-10:45am':{'course':'ECE342','name':'Applied Model Based Design','section':'','year':'III/EEE','faculty':'Dr. A. Sridevi'},'11:00am-12:00noon':{'course':'EIE114','name':'Feedback Control Systems','section':'','year':'II/EEE','faculty':'Dr.S. Augustii Lindiya'},'12:00noon-1:00pm':{'course':'EIE114','name':'Feedback Control Systems','section':'','year':'II/EEE','faculty':'Dr.S. Augustii Lindiya'},'1:00pm-2:00pm':{'course':'ECE507','name':'RF Communication Lab','section':'','year':'M.Tech WSC','faculty':'Dr.R.Vijay Sai'},'2:00pm-3:00pm':{'course':'ECE507','name':'RF Communication Lab','section':'','year':'M.Tech WSC','faculty':'Dr.R.Vijay Sai'}},
        'Tuesday':   {'11:00am-12:00noon':{'course':'ECE317','name':'FPGA Laboratory','section':'','year':'III/ECE/CPS','faculty':'Dr.S. Archana'},'12:00noon-1:00pm':{'course':'ECE317','name':'FPGA Laboratory','section':'','year':'III/ECE/CPS','faculty':'Dr.S. Archana'},'1:00pm-2:00pm':{'course':'ECE313','name':'DSP & Architecture Lab','section':'','year':'III/ICT','faculty':'Dr.R.Vijay Sai'},'2:00pm-3:00pm':{'course':'ECE313','name':'DSP & Architecture Lab','section':'','year':'III/ICT','faculty':'Dr.R.Vijay Sai'},'3:15pm-4:15pm':{'course':'ECE327','name':'FPGA System Design Lab','section':'','year':'III/ECE/VLSI','faculty':'Dr.T N Prabakar'},'4:15pm-5:15pm':{'course':'ECE327','name':'FPGA System Design Lab','section':'','year':'III/ECE/VLSI','faculty':'Dr.T N Prabakar'}},
        'Wednesday': {'11:00am-12:00noon':{'course':'EIE114','name':'Feedback Control Systems','section':'P3-4','year':'II/EEE','faculty':'Dr.S. Augustii Lindiya'},'12:00noon-1:00pm':{'course':'EIE114','name':'Feedback Control Systems','section':'P3-4','year':'II/EEE','faculty':'Dr.S. Augustii Lindiya'},'1:00pm-2:00pm':{'course':'EIE114','name':'Feedback Control Systems','section':'P5-6','year':'III/EEE','faculty':'Dr.S. Augustii Lindiya'},'2:00pm-3:00pm':{'course':'EIE114','name':'Feedback Control Systems','section':'P5-6','year':'III/EEE','faculty':'Dr.S. Augustii Lindiya'},'3:15pm-4:15pm':{'course':'ECE317','name':'FPGA Laboratory','section':'F2','year':'III/ECE/CPS','faculty':'Dr.S. Archana'},'4:15pm-5:15pm':{'course':'ECE317','name':'FPGA Laboratory','section':'F2','year':'III/ECE/CPS','faculty':'Dr.S. Archana'}},
        'Thursday':  {'8:45am-9:45am':{'course':'ECE507','name':'RF Communication Lab','section':'','year':'M.Tech WSC','faculty':'Dr.R.Vijay Sai'},'9:45am-10:45am':{'course':'ECE507','name':'RF Communication Lab','section':'','year':'M.Tech WSC','faculty':'Dr.R.Vijay Sai'},'11:00am-12:00noon':{'course':'ECE327','name':'FPGA System Design Lab','section':'','year':'III/ECE/VLSI','faculty':'Dr.T N Prabakar'},'12:00noon-1:00pm':{'course':'ECE327','name':'FPGA System Design Lab','section':'','year':'III/ECE/VLSI','faculty':'Dr.T N Prabakar'}},
        'Friday':    {'8:45am-9:45am':{'course':'ECE335','name':'FPGA Architecture','section':'','year':'III/ECE','faculty':'Dr.James A Baskaradas'},'9:45am-10:45am':{'course':'ECE335','name':'FPGA Architecture','section':'','year':'III/ECE','faculty':'Dr.James A Baskaradas'},'11:00am-12:00noon':{'course':'ECE317','name':'FPGA Laboratory','section':'G2','year':'III/ECE/VLSI','faculty':'Dr.T N Prabakar'},'12:00noon-1:00pm':{'course':'ECE317','name':'FPGA Laboratory','section':'G2','year':'III/ECE/VLSI','faculty':'Dr.T N Prabakar'},'1:00pm-2:00pm':{'course':'ECE313','name':'DSP & Architecture Lab','section':'','year':'III/ICT','faculty':'Dr.R.Vijay Sai'},'2:00pm-3:00pm':{'course':'ECE313','name':'DSP & Architecture Lab','section':'','year':'III/ICT','faculty':'Dr.R.Vijay Sai'}},
        'Saturday':{},'Sunday':{}
    },
    'ai': {
        'Monday':    {'12:00noon-1:00pm':{'course':'EIE331K','name':'Generative AI & Machine Intelligence','section':'','year':'III/VI','faculty':'Dr.T. Venkatesh'}},
        'Tuesday':   {'1:00pm-2:00pm':{'course':'EEE307','name':'EEE307','section':'I2','year':'','faculty':''},'2:00pm-3:00pm':{'course':'EEE307','name':'EEE307','section':'I2','year':'','faculty':''},'3:15pm-4:15pm':{'course':'EEE307','name':'EEE307','section':'','year':'','faculty':''},'4:15pm-5:15pm':{'course':'EEE307','name':'EEE307','section':'','year':'','faculty':''}},
        'Wednesday': {'8:45am-9:45am':{'course':'EIE331','name':'EIE331','section':'','year':'','faculty':''},'11:00am-12:00noon':{'course':'EEE331','name':'EEE331-K2','section':'K2','year':'','faculty':''},'12:00noon-1:00pm':{'course':'EEE331','name':'EEE331-K2','section':'K2','year':'','faculty':''},'3:15pm-4:15pm':{'course':'IEE507','name':'IEE507','section':'','year':'','faculty':''},'4:15pm-5:15pm':{'course':'IEE507','name':'IEE507','section':'','year':'','faculty':''}},
        'Thursday':  {'8:45am-9:45am':{'course':'EIE115R01','name':'EIE115R01-K2','section':'K2','year':'','faculty':''},'9:45am-10:45am':{'course':'EIE115R01','name':'EIE115R01-K2','section':'K2','year':'','faculty':''},'11:00am-12:00noon':{'course':'EIE331K','name':'Generative AI','section':'K2','year':'','faculty':'Dr.T. Venkatesh'},'12:00noon-1:00pm':{'course':'EIE331K','name':'Generative AI','section':'K2','year':'','faculty':'Dr.T. Venkatesh'},'3:15pm-4:15pm':{'course':'ECE335','name':'FPGA Architecture','section':'','year':'III/ECE','faculty':'Dr.N. Mohanraj'},'4:15pm-5:15pm':{'course':'ECE335','name':'FPGA Architecture','section':'','year':'III/ECE','faculty':'Dr.N. Mohanraj'}},
        'Friday':    {'8:45am-9:45am':{'course':'ECE335','name':'FPGA Architecture','section':'','year':'III/ECE','faculty':'Dr.N. Mohanraj'},'9:45am-10:45am':{'course':'ECE335','name':'FPGA Architecture','section':'','year':'III/ECE','faculty':'Dr.N. Mohanraj'},'3:15pm-4:15pm':{'course':'EEE307','name':'EEE307-H2','section':'H2','year':'','faculty':''},'4:15pm-5:15pm':{'course':'EIE608','name':'Autonomous Navigation','section':'','year':'','faculty':''}},
        'Saturday':{},'Sunday':{}
    }
}

# ── In-memory store (Firebase fallback) ───────────────────────────────────────
_mem_bookings = {}; _mem_waitlist = {}; _mem_audit = []; _mem_counter = [0]
_USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'users_data.json')

def _load_users():
    """Load users from JSON file on disk (survives server restarts)."""
    try:
        if os.path.exists(_USERS_FILE):
            with open(_USERS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"⚠️ Could not load users file: {e}")
    return {}

def _save_users():
    """Persist _mem_users to disk (only when Firebase is NOT available)."""
    if db:
        return  # Firebase is active — no need to write local users_data.json
    try:
        with open(_USERS_FILE, 'w') as f:
            json.dump(_mem_users, f, indent=2)
    except Exception as e:
        print(f"⚠️ Could not save users file: {e}")

_mem_users = _load_users()
print(f"👥 Loaded {len(_mem_users)} user(s) from disk.")

def _new_id():
    _mem_counter[0]+=1; return f"MEM{_mem_counter[0]:06d}{secrets.token_hex(4)}"

# ── Helpers ───────────────────────────────────────────────────────────────────
def hash_password(p):
    if BCRYPT_AVAILABLE: return bcrypt.hashpw(p.encode(),bcrypt.gensalt()).decode()
    return hashlib.sha256(p.encode()).hexdigest()

def verify_password(p,h):
    if not h or not p: return False
    # Try bcrypt first
    if BCRYPT_AVAILABLE and h.startswith('$2'):
        try: return bcrypt.checkpw(p.encode(),h.encode())
        except: pass
    # Try SHA256
    if hashlib.sha256(p.encode()).hexdigest() == h:
        return True
    # Last resort: plain text match (for legacy accounts)
    if p == h:
        return True
    return False

def verify_fallback_user(username, password):
    u=FALLBACK_USERS.get(username)
    if u and u['password_plain']==password: return {k:v for k,v in u.items() if k!='password_plain'}
    return None

def audit_log(action,user_id,details):
    entry={'action':action,'userId':user_id,'details':details,'timestamp':datetime.now().isoformat()}
    if db:
        try: db.collection('audit_log').document().set(entry)
        except: pass
    else: _mem_audit.append(entry)

_rate_limit={}
def check_rate_limit(user_id,max_per_hour=10):
    now=datetime.now(); hour_ago=now-timedelta(hours=1)
    _rate_limit.setdefault(user_id,[])
    _rate_limit[user_id]=[t for t in _rate_limit[user_id] if t>hour_ago]
    if len(_rate_limit[user_id])>=max_per_hour: return False
    _rate_limit[user_id].append(now); return True

# ── DB wrappers ───────────────────────────────────────────────────────────────
def get_bookings_for_slot(lab,date,tslot,statuses=('pending','approved')):
    results=[]
    if db:
        try:
            for doc in db.collection('bookings').where('lab','==',lab).where('date','==',date).where('time','==',tslot).where('status','in',list(statuses)).stream():
                b=doc.to_dict(); b['id']=doc.id; results.append(b)
        except: pass
    else:
        for bid,b in _mem_bookings.items():
            if b['lab']==lab and b['date']==date and b['time']==tslot and b['status'] in statuses:
                results.append({**b,'id':bid})
    return results

def get_all_bookings(status_filter='all'):
    results=[]
    if db:
        try:
            q=db.collection('bookings')
            if status_filter!='all': q=q.where('status','==',status_filter)
            for doc in q.stream(): b=doc.to_dict(); b['id']=doc.id; results.append(b)
        except: pass
    else:
        for bid,b in _mem_bookings.items():
            if status_filter=='all' or b['status']==status_filter: results.append({**b,'id':bid})
    return sorted(results,key=lambda x:x.get('createdAt',''),reverse=True)

def get_user_bookings(user_id):
    results=[]
    if db:
        try:
            for doc in db.collection('bookings').where('userId','==',user_id).stream():
                b=doc.to_dict(); b['id']=doc.id; results.append(b)
        except: pass
    else:
        for bid,b in _mem_bookings.items():
            if b['userId']==user_id: results.append({**b,'id':bid})
    return sorted(results,key=lambda x:x.get('createdAt',''),reverse=True)

def save_booking(data):
    if db:
        ref=db.collection('bookings').document(); ref.set(data); return ref.id
    else: bid=_new_id(); _mem_bookings[bid]={**data,'id':bid}; return bid

def update_booking(bid,updates):
    if db:
        try: db.collection('bookings').document(bid).update(updates)
        except: pass
    else:
        if bid in _mem_bookings: _mem_bookings[bid].update(updates)

def get_booking(bid):
    if db:
        try:
            d=db.collection('bookings').document(bid).get()
            if d.exists: b=d.to_dict(); b['id']=bid; return b
        except: pass
    else: return _mem_bookings.get(bid)
    return None

def get_user_data(user_id):
    if db:
        try:
            d=db.collection('users').document(user_id).get()
            if d.exists: return d.to_dict()
            for doc in db.collection('users').where('username','==',user_id).limit(1).stream():
                return doc.to_dict()
        except: pass
    u=FALLBACK_USERS.get(user_id)
    if u: return {k:v for k,v in u.items() if k!='password_plain'}
    if user_id in _mem_users: return _mem_users[user_id]
    for uid,udata in _mem_users.items():
        if udata.get('username')==user_id: return udata
    return None

# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(to,subject,html):
    if not MAIL_AVAILABLE or not mail:
        print(f"📧 [skipped — email not configured] To:{to} | {subject}"); return
    # Skip if credentials are still placeholder
    if EMAIL_USER in ('your_email@gmail.com',) or not EMAIL_PASSWORD or EMAIL_PASSWORD in ('your_app_password','xxxx xxxx xxxx xxxx'):
        print(f"📧 [skipped — set EMAIL_USER and EMAIL_PASSWORD to enable] To:{to} | {subject}"); return
    def _s():
        try:
            with app.app_context(): mail.send(Message(subject=subject,recipients=[to],html=html)); print(f"📧 Sent→{to}")
        except Exception as e: print(f"📧 Error: {e}\n   → To fix: use a personal Gmail + App Password (see README)")
    threading.Thread(target=_s,daemon=True).start()

def _row(label,val,bg='#f8f9fa'):
    return f'<tr><td style="padding:8px;background:{bg};font-weight:bold;">{label}</td><td style="padding:8px;">{val}</td></tr>'

def _qr_email_block(booking):
    """Returns inline PNG QR block for emails with full booking details."""
    qr_uri = make_qr_image_b64(booking)
    bid    = str(booking.get('id',''))
    name   = booking.get('userName','')
    lab    = LABS.get(booking.get('lab',''),{}).get('name', booking.get('lab',''))
    pcs    = ', '.join(map(str, sorted(booking.get('computers',[]))))
    return f'''<div style="text-align:center;margin:20px 0;font-family:Arial">
        <p style="font-weight:bold;margin-bottom:8px;font-size:15px">📱 Show this QR code at the lab entrance</p>
        <div style="display:inline-block;padding:14px;border:3px solid #667eea;border-radius:12px;background:white">
          <img src="{qr_uri}" width="200" height="200" style="display:block" alt="Booking QR">
        </div>
        <p style="font-size:12px;color:#444;margin-top:8px">
          <b>{name}</b> &nbsp;·&nbsp; {lab}<br>
          💻 Computers: {pcs}<br>
          <span style="color:#888">Booking ID: {bid[:16]}</span>
        </p>
    </div>'''

def email_received(email,name,bid,lab,date,slot):
    send_email(email,"📋 Lab Booking Received – SASTRA",f"""
    <div style="font-family:Arial;max-width:600px;margin:auto;padding:20px;border:1px solid #ddd;border-radius:10px;">
    <h2 style="color:#667eea;">📋 Booking Received</h2>
    <p>Hi <b>{name}</b>, your booking is under review. ML auto-approval runs every 5 minutes.</p>
    <table style="width:100%;border-collapse:collapse;margin-top:15px;">
    {_row('Booking ID',str(bid)[:12])}{_row('Lab',lab)}{_row('Date',date)}{_row('Time',slot)}
    </table><p style="color:#667eea;font-weight:bold;margin-top:15px;">SASTRA Lab Booking</p></div>""")

def email_approved(email,name,bid,lab,date,slot,computers,by,booking=None):
    qr_block = _qr_email_block(booking) if booking else ''
    send_email(email,"✅ Booking Approved – SASTRA",f"""
    <div style="font-family:Arial;max-width:600px;margin:auto;padding:20px;border:2px solid #28a745;border-radius:10px;">
    <h2 style="color:#28a745;">✅ Booking Approved!</h2><p>Hi <b>{name}</b>, your booking is <b>APPROVED</b>.</p>
    <table style="width:100%;border-collapse:collapse;margin-top:15px;">
    {_row('Lab',lab,'#d4edda')}{_row('Date',date,'#d4edda')}{_row('Time',slot,'#d4edda')}
    {_row('Computers',', '.join(map(str,computers)),'#d4edda')}{_row('Approved By',by,'#d4edda')}
    </table>
    {qr_block}
    <p style="margin-top:15px;background:#fff3cd;padding:10px;border-radius:6px;">⏰ A reminder will be sent 30 min before your session.</p>
    <p style="color:#667eea;font-weight:bold;">SASTRA Lab Booking</p></div>""")

def email_rejected(email,name,lab,date,slot,reason,by):
    send_email(email,"❌ Booking Rejected – SASTRA",f"""
    <div style="font-family:Arial;max-width:600px;margin:auto;padding:20px;border:2px solid #dc3545;border-radius:10px;">
    <h2 style="color:#dc3545;">❌ Booking Rejected</h2><p>Hi <b>{name}</b>, your booking was rejected.</p>
    <table style="width:100%;border-collapse:collapse;margin-top:15px;">
    {_row('Lab',lab,'#f8d7da')}{_row('Date',date,'#f8d7da')}{_row('Time',slot,'#f8d7da')}
    {_row('Rejected By',by,'#f8d7da')}{_row('Reason',reason,'#f8d7da')}
    </table><p style="color:#667eea;font-weight:bold;margin-top:15px;">SASTRA Lab Booking</p></div>""")

def email_cancelled(email,name,lab,date,slot):
    send_email(email,"🚫 Booking Cancelled – SASTRA",f"""
    <div style="font-family:Arial;max-width:600px;margin:auto;padding:20px;border:2px solid #6c757d;border-radius:10px;">
    <h2 style="color:#6c757d;">🚫 Booking Cancelled</h2><p>Hi <b>{name}</b>, your booking has been cancelled and computers released.</p>
    <table style="width:100%;border-collapse:collapse;margin-top:15px;">
    {_row('Lab',lab)}{_row('Date',date)}{_row('Time',slot)}
    </table><p style="color:#667eea;font-weight:bold;">SASTRA Lab Booking</p></div>""")

def email_reminder(email,name,lab,date,slot,computers):
    send_email(email,"⏰ 30-Min Reminder – SASTRA",f"""
    <div style="font-family:Arial;max-width:600px;margin:auto;padding:20px;border:2px solid #ffc107;border-radius:10px;">
    <h2 style="color:#856404;">⏰ Session in 30 Minutes!</h2><p>Hi <b>{name}</b>.</p>
    <table style="width:100%;border-collapse:collapse;margin-top:15px;">
    {_row('Lab',lab,'#fff3cd')}{_row('Date',date,'#fff3cd')}{_row('Time',slot,'#fff3cd')}
    {_row('Your PCs',', '.join(map(str,computers)),'#fff3cd')}
    </table><p style="color:#667eea;font-weight:bold;">SASTRA Lab Booking</p></div>""")

def email_waitlist_promoted(email,name,lab,date,slot):
    send_email(email,"🎉 Waitlist Slot Available – SASTRA",f"""
    <div style="font-family:Arial;max-width:600px;margin:auto;padding:20px;border:2px solid #667eea;border-radius:10px;">
    <h2 style="color:#667eea;">🎉 A Slot Is Now Available!</h2>
    <p>Hi <b>{name}</b>, a booking was cancelled — login now to book before it's taken.</p>
    <table style="width:100%;border-collapse:collapse;margin-top:15px;">
    {_row('Lab',lab)}{_row('Date',date)}{_row('Time',slot)}
    </table><p style="color:#667eea;font-weight:bold;">SASTRA Lab Booking</p></div>""")

# ── Password Reset Token Store ─────────────────────────────────────────────────
_reset_tokens = {}   # token -> {'email': ..., 'expires': datetime}

def email_password_reset(email, name, token):
    reset_url = f"http://localhost:5000/reset-password?token={token}"
    send_email(email, "🔑 Password Reset – SASTRA Lab Booking", f"""
    <div style="font-family:Arial;max-width:600px;margin:auto;padding:28px;border:2px solid #667eea;border-radius:14px;">
      <h2 style="color:#667eea;">🔑 Reset Your Password</h2>
      <p>Hi <b>{name if name else 'there'}</b>,</p>
      <p>We received a request to reset your SASTRA Lab Booking password. Click the button below to set a new password. This link is valid for <b>30 minutes</b>.</p>
      <div style="text-align:center;margin:28px 0;">
        <a href="{reset_url}" style="background:#667eea;color:white;padding:14px 32px;border-radius:10px;text-decoration:none;font-weight:700;font-size:1.05em;">
          🔒 Set New Password
        </a>
      </div>
      <p style="font-size:.86em;color:#888;">If you did not request a password reset, you can safely ignore this email.</p>
      <p style="font-size:.86em;color:#888;">Or copy this link into your browser:<br>
        <a href="{reset_url}" style="color:#667eea;word-break:break-all;">{reset_url}</a></p>
      <p style="color:#667eea;font-weight:bold;margin-top:20px;">SASTRA Lab Booking System</p>
    </div>""")

# ── Waitlist ──────────────────────────────────────────────────────────────────
def _notify_waitlist(lab,date,tslot):
    if db:
        try:
            for doc in db.collection('waitlist').where('lab','==',lab).where('date','==',date).where('time','==',tslot).where('notified','==',False).limit(1).stream():
                w=doc.to_dict()
                email_waitlist_promoted(w.get('userEmail',''),w.get('userName',''),LABS.get(lab,{}).get('name',lab),date,tslot)
                sse_push(w.get('userId',''),'waitlist',{'message':f"🎉 A slot opened up in {lab} on {date}!"})
                db.collection('waitlist').document(doc.id).update({'notified':True,'notifiedAt':datetime.now().isoformat()})
                break
        except: pass
    else:
        for wid,w in _mem_waitlist.items():
            if w['lab']==lab and w['date']==date and w['time']==tslot and not w.get('notified'):
                email_waitlist_promoted(w.get('userEmail',''),w.get('userName',''),LABS.get(lab,{}).get('name',lab),date,tslot)
                sse_push(w.get('userId',''),'waitlist',{'message':f"🎉 A slot opened up in {lab} on {date}!"})
                w['notified']=True; break

# ── Background workers ─────────────────────────────────────────────────────────
def auto_approval_worker():
    while True:
        try:
            now=datetime.now(); cutoff=now-timedelta(minutes=5)
            for b in get_all_bookings('pending'):
                try: requested_at=datetime.fromisoformat(b.get('requestedAt',now.isoformat()))
                except: requested_at=now
                if requested_at>=cutoff: continue
                ns_rate,total=ml_engine.get_user_stats(db,b.get('userId',''))
                ml=ml_engine.predict(lab=b.get('lab',''),date=b.get('date',''),time_slot=b.get('time',''),
                    num_computers=len(b.get('computers',[])),purpose=b.get('purpose',''),
                    user_role=b.get('userRole','student'),noshow_rate=ns_rate,total_bookings=total)
                if ml['recommendation']=='auto_approve':
                    bid=b['id']
                    update_booking(bid,{'status':'approved','approvedBy':'ML Auto-Approval',
                        'approvedAt':now.isoformat(),'ml_confidence_at_approval':ml['xgb_confidence']})
                    lab_name=LABS.get(b.get('lab',''),{}).get('name',b.get('lab',''))
                    full_b=get_booking(bid)
                    email_approved(b.get('userEmail',''),b.get('userName',''),bid,lab_name,
                        b.get('date',''),b.get('time',''),b.get('computers',[]),'ML Auto-Approval',full_b)
                    # Push notification to user
                    sse_push(b.get('userId',''),'approved',{
                        'title':'✅ Booking Approved!',
                        'message':f"Your booking for {lab_name} on {b.get('date','')} was auto-approved by ML.",
                        'bookingId':bid})
                    audit_log('AUTO_APPROVED',b.get('userId',''),{'bookingId':bid,'confidence':ml['xgb_confidence']})

            # 30-min reminders
            rws=now+timedelta(minutes=25); rwe=now+timedelta(minutes=35)
            for b in get_all_bookings('approved'):
                if b.get('reminderSent'): continue
                try:
                    st=SLOT_START_TIMES.get(b.get('time',''));
                    if not st: continue
                    sdt=datetime.strptime(f"{b.get('date','')} {st}",'%Y-%m-%d %H:%M')
                    if rws<=sdt<=rwe:
                        lab_name=LABS.get(b.get('lab',''),{}).get('name',b.get('lab',''))
                        email_reminder(b.get('userEmail',''),b.get('userName',''),lab_name,b.get('date',''),b.get('time',''),b.get('computers',[]))
                        sse_push(b.get('userId',''),'reminder',{'title':'⏰ Session in 30 mins!','message':f"{lab_name} — {b.get('time','')}",'bookingId':b['id']})
                        update_booking(b['id'],{'reminderSent':True})
                except: continue
            time.sleep(30)
        except Exception as e: print(f"Worker error: {e}"); time.sleep(30)

def ml_retrain_worker():
    time.sleep(300)
    while True:
        try: ml_engine.train(db_ref=db)
        except Exception as e: print(f"Retrain error: {e}")
        time.sleep(86400)

threading.Thread(target=auto_approval_worker,daemon=True).start()
threading.Thread(target=ml_retrain_worker,daemon=True).start()

def init_firebase():
    if not db: return
    try:
        db.collection('users').document('admin').set({'username':'admin','password':hash_password('admin123'),'email':'admin@sastra.edu','name':'System Administrator','role':'admin','department':'Administration','createdAt':datetime.now().isoformat()})
        db.collection('users').document('faculty1').set({'username':'faculty1','password':hash_password('faculty123'),'email':'faculty@sastra.edu','name':'Dr. Faculty Member','role':'faculty','department':'ECE','createdAt':datetime.now().isoformat()})
        print("✅ Default accounts ready")
    except Exception as e: print(f"Firebase init error: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
#  HTML TEMPLATE
# ═══════════════════════════════════════════════════════════════════════════════
HTML = r'''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SASTRA Lab Booking v4</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;padding:20px}
.container{max-width:1400px;margin:0 auto;background:white;border-radius:20px;padding:28px;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.header{text-align:center;margin-bottom:30px;padding-bottom:18px;border-bottom:3px solid #667eea}
.header h1{color:#667eea;font-size:2.2em;margin-bottom:8px}
.badge{display:inline-block;background:linear-gradient(135deg,#11998e,#38ef7d);color:white;padding:3px 10px;border-radius:20px;font-size:.75em;font-weight:700;margin:2px}
/* Toast */
#toastContainer{position:fixed;top:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:10px}
.toast{padding:14px 20px;border-radius:10px;color:white;font-weight:600;font-size:.92em;box-shadow:0 4px 15px rgba(0,0,0,.2);animation:slideIn .3s ease;max-width:340px;cursor:pointer}
.toast.success{background:#28a745}.toast.error{background:#dc3545}.toast.info{background:#667eea}.toast.warning{background:#ffc107;color:#333}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
/* Notification banner */
#notifBanner{background:linear-gradient(135deg,#667eea,#764ba2);color:white;padding:10px 16px;border-radius:10px;margin-bottom:16px;display:none;align-items:center;justify-content:space-between;gap:12px}
#notifBanner.show{display:flex}
/* Auth */
.auth-box{max-width:440px;margin:40px auto;background:#f8f9fa;padding:34px;border-radius:15px}
.form-group{margin-bottom:15px}
.form-group label{display:block;margin-bottom:5px;font-weight:600;color:#333;font-size:.92em}
.form-group input,.form-group select,.form-group textarea{width:100%;padding:10px 12px;border:2px solid #ddd;border-radius:8px;font-size:.95em;transition:border .2s}
.form-group input:focus,.form-group select:focus{border-color:#667eea;outline:none}
.btn{width:100%;padding:12px;border:none;border-radius:10px;background:#667eea;color:white;font-size:1em;font-weight:600;cursor:pointer;transition:all .2s}
.btn:hover{background:#5568d3;transform:translateY(-1px)}.btn:disabled{background:#ccc;cursor:not-allowed;transform:none}
.err-msg{background:#f8d7da;color:#721c24;padding:10px;border-radius:7px;margin-top:10px;display:none;font-size:.88em}
.pw-wrap{position:relative;display:flex;align-items:center}
.pw-wrap input{flex:1;padding-right:42px}
.pw-eye{position:absolute;right:10px;background:none;border:none;cursor:pointer;font-size:1.15em;color:#888;padding:0;line-height:1;user-select:none}
.pw-eye:hover{color:#667eea}
.info-note{background:#d1ecf1;color:#0c5460;padding:10px;border-radius:7px;margin-bottom:12px;font-size:.82em;border-left:4px solid #667eea}
/* User bar */
.user-bar{background:#f8f9fa;padding:16px 20px;border-radius:10px;margin-bottom:22px;display:flex;justify-content:space-between;align-items:center}
.avatar{width:44px;height:44px;border-radius:50%;background:#667eea;display:flex;align-items:center;justify-content:center;color:white;font-size:1.3em;font-weight:bold}
.logout-btn{padding:8px 16px;background:#dc3545;color:white;border:none;border-radius:8px;cursor:pointer;font-weight:600}
.notif-btn{padding:8px 14px;background:#667eea;color:white;border:none;border-radius:8px;cursor:pointer;font-weight:600;font-size:.85em;margin-right:8px}
/* Nav */
.nav-tabs{display:flex;gap:7px;margin-bottom:22px;flex-wrap:wrap}
.nav-tab{padding:9px 20px;border:2px solid #667eea;background:white;color:#667eea;border-radius:25px;cursor:pointer;font-weight:600;transition:all .2s}
.nav-tab.active,.nav-tab:hover{background:#667eea;color:white}
/* Cards */
.card{background:#f8f9fa;padding:20px;border-radius:14px;margin-bottom:20px}
.card-title{font-size:1.25em;color:#333;margin-bottom:16px;display:flex;align-items:center;gap:8px}
/* Lab grid */
.lab-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:13px}
.lab-card{background:white;padding:17px;border-radius:12px;cursor:pointer;border:3px solid transparent;transition:all .25s}
.lab-card:hover{border-color:#667eea;transform:translateY(-4px)}
.lab-card.selected{border-color:#667eea;background:linear-gradient(135deg,#667eea,#764ba2);color:white}
/* Slots */
.slots-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:9px;margin-top:14px}
.slot{padding:12px;border-radius:9px;text-align:center;cursor:pointer;font-weight:600;transition:all .2s;border:2px solid transparent;font-size:.9em}
.slot.free{background:#d4edda;color:#155724;border-color:#c3e6cb}
.slot.free:hover{transform:scale(1.03)}
.slot.blocked{background:#f8d7da;color:#721c24;cursor:help}
.slot.selected{background:#667eea;color:white;transform:scale(1.03)}
.demand-tag{font-size:.65em;display:block;margin-top:2px}
.dHigh{color:#dc3545}.dMed{color:#e67e22}.dLow{color:#27ae60}
/* Seat map */
.seat-map{display:grid;grid-template-columns:repeat(auto-fill,minmax(74px,1fr));gap:9px;margin-top:14px}
.seat{aspect-ratio:1;border-radius:10px;display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:pointer;transition:all .2s;border:3px solid transparent;position:relative}
.seat.free{background:#d4edda;color:#155724}
.seat.free:hover{border-color:#28a745;transform:scale(1.08)}
.seat.selected{background:#667eea;color:white;transform:scale(1.08)}
.seat.locked{background:#f8d7da;color:#721c24;cursor:not-allowed;opacity:.8}
.seat.my-booked{background:#fff3cd;color:#856404;cursor:not-allowed}
.seat-icon{font-size:1.8em}.seat-num{font-size:.78em;font-weight:700;margin-top:2px}
.seat-owner{position:absolute;bottom:2px;font-size:.55em;color:inherit;opacity:.8;text-align:center;padding:0 2px}
.seat-legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:10px;font-size:.8em}
.legend-item{display:flex;align-items:center;gap:5px}
.legend-dot{width:14px;height:14px;border-radius:3px}
/* ML card */
.ml-card{background:linear-gradient(135deg,#1a1a2e,#16213e);color:white;padding:16px;border-radius:11px;margin-top:13px;display:none}
.ml-card.show{display:block}
.ml-title{font-size:1em;font-weight:700;margin-bottom:12px;color:#38ef7d}
.ml-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:9px}
.ml-box{background:rgba(255,255,255,.08);padding:10px;border-radius:7px;text-align:center}
.ml-val{font-size:1.4em;font-weight:800}.ml-lbl{font-size:.68em;opacity:.7;margin-top:2px}
.ml-reason{margin-top:10px;padding:8px;background:rgba(255,255,255,.06);border-radius:7px;font-size:.84em;line-height:1.5}
.countdown{text-align:center;margin-top:10px;font-size:.82em;color:#38ef7d}
/* QR code */
.qr-block{background:white;border:2px solid #667eea;border-radius:12px;padding:14px;text-align:center;margin-top:12px}
.qr-block h4{color:#667eea;margin-bottom:8px;font-size:.95em}
.qr-block small{display:block;color:#666;font-size:.75em;margin-top:6px}
.qr-verify-input{width:100%;padding:8px;border:2px solid #ddd;border-radius:6px;margin:8px 0;font-size:.88em}
/* Booking cards */
.booking-card{background:white;padding:17px;border-radius:10px;margin-bottom:12px;border-left:5px solid #667eea}
.booking-card.approved{border-left-color:#28a745}
.booking-card.rejected{border-left-color:#dc3545}
.booking-card.pending{border-left-color:#ffc107}
.booking-card.cancelled{border-left-color:#6c757d;opacity:.75}
.booking-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px}
.status-badge{padding:4px 11px;border-radius:20px;font-size:.82em;font-weight:700}
.status-badge.approved{background:#d4edda;color:#155724}
.status-badge.rejected{background:#f8d7da;color:#721c24}
.status-badge.pending{background:#fff3cd;color:#856404}
.status-badge.cancelled{background:#e9ecef;color:#495057}
.booking-row{padding:5px 0;border-bottom:1px solid #f0f0f0;font-size:.88em}
.booking-lbl{font-weight:600;color:#666;display:inline-block;min-width:120px}
.ml-mini{margin-top:9px;padding:8px 11px;background:#f0f8ff;border-radius:7px;border-left:3px solid #667eea;font-size:.82em}
.action-row{display:flex;gap:8px;margin-top:11px;flex-wrap:wrap}
.act-btn{flex:1;min-width:80px;padding:8px;border:none;border-radius:7px;font-weight:600;cursor:pointer;font-size:.88em}
.cancel-btn{background:#6c757d;color:white}.approve-btn{background:#28a745;color:white}
.reject-btn{background:#dc3545;color:white}.noshow-btn{background:#fd7e14;color:white}
.qr-btn{background:#667eea;color:white}
/* Admin */
.stats-bar{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}
.stat{background:linear-gradient(135deg,#667eea,#764ba2);color:white;padding:14px;border-radius:11px;text-align:center}
.stat-val{font-size:1.8em;font-weight:800}.stat-lbl{font-size:.72em;opacity:.8;margin-top:3px}
.filter-btns{margin-bottom:16px}
.filter-btn{padding:8px 16px;border:2px solid #667eea;background:white;color:#667eea;border-radius:8px;cursor:pointer;font-weight:600;margin:0 6px 6px 0}
.filter-btn.active{background:#667eea;color:white}
.retrain-info{background:#f0fff0;border:2px solid #28a745;border-radius:9px;padding:14px;margin-bottom:18px;font-size:.86em}
/* Calendar */
.cal-controls{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.cal-nav{padding:7px 14px;border:2px solid #667eea;background:white;color:#667eea;border-radius:7px;cursor:pointer;font-weight:600}
.cal-nav:hover{background:#667eea;color:white}
table.cal{width:100%;border-collapse:collapse;min-width:680px;font-size:.82em}
table.cal th{background:#667eea;color:white;padding:9px 5px;text-align:center}
table.cal td{border:1px solid #e0e0e0;vertical-align:top;padding:3px;min-width:90px}
td.slot-label{background:#f8f9fa;font-weight:600;font-size:.76em;color:#444;width:120px;padding:7px 5px;vertical-align:middle}
.cal-block{border-radius:4px;padding:2px 5px;margin:2px 0;font-size:.74em;line-height:1.3}
.cal-tt{background:#f8d7da;color:#721c24}.cal-approved{background:#d4edda;color:#155724}
.cal-pending{background:#fff3cd;color:#856404}.cal-free{background:#e8f5e9;color:#388e3c;text-align:center;padding:4px}
.cal-legend{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;font-size:.78em}
.cal-legend span{padding:2px 8px;border-radius:4px}
/* Waitlist */
.waitlist-form{background:#f0f8ff;border:2px solid #667eea;border-radius:9px;padding:14px;margin-top:10px}
/* Success */
.success-msg{display:none;background:#d4edda;color:#155724;padding:14px;border-radius:9px;border-left:5px solid #28a745;margin-top:13px}
.success-msg.show{display:block}
/* Modal */
.modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:1000;align-items:center;justify-content:center}
.modal.show{display:flex}
.modal-box{background:white;padding:26px;border-radius:14px;max-width:500px;width:90%;max-height:85vh;overflow-y:auto}
.info-row{display:flex;justify-content:space-between;padding:8px;background:#f8f9fa;border-radius:7px;margin-bottom:7px}
</style>
</head>
<body>
<div id="toastContainer"></div>
<div class="container">
  <div class="header">
    <h1>🎓 SASTRA Lab Booking <small style="font-size:.45em;color:#764ba2;">v4</small></h1>
    <span class="badge">🤖 XGBoost ML</span><span class="badge">🔒 Seat Locking</span>
    <span class="badge">📱 QR Code</span><span class="badge">🔔 Push Notifications</span>
    <span class="badge">📧 Email Alerts</span><span class="badge">📋 Waitlist</span>
  </div>

  <!-- Notification permission banner -->
  <div id="notifBanner">
    <span>🔔 Enable browser notifications to get instant alerts when your booking is approved or rejected!</span>
    <div style="display:flex;gap:8px;flex-shrink:0">
      <button onclick="requestNotifPermission()" style="padding:6px 14px;background:white;color:#667eea;border:none;border-radius:6px;font-weight:700;cursor:pointer">Enable</button>
      <button onclick="dismissNotifBanner()" style="padding:6px 10px;background:rgba(255,255,255,.2);color:white;border:none;border-radius:6px;cursor:pointer">✕</button>
    </div>
  </div>

  <!-- AUTH -->
  <div id="authSection" class="auth-box">
    <div class="info-note">🔑 Default logins: <b>admin / admin123</b> · <b>faculty1 / faculty123</b></div>
    <form id="loginForm">
      <h2 style="text-align:center;margin-bottom:16px;">Login</h2>
      <div class="form-group"><label>Username</label><input id="lu" type="text" required autocomplete="username"></div>
      <div class="form-group"><label>Password</label><div class="pw-wrap"><input id="lp" type="password" required autocomplete="current-password"><button type="button" class="pw-eye" onclick="togglePw('lp',this)" title="Show/hide password">👁️</button></div></div>
      <button type="submit" class="btn" id="loginBtn">Login</button>
      <div style="text-align:center;margin-top:10px;font-size:.88em"><a href="#" onclick="showForgotPassword()" style="color:#667eea">Forgot password?</a></div>
      <div style="text-align:center;margin-top:8px;font-size:.88em">No account? <a href="#" onclick="googleLogin()" style="color:#667eea;font-weight:600;display:flex;align-items:center;justify-content:center;gap:6px;margin-top:4px"><img src="https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg" width="16"> Sign in with Google</a></div>
      <div id="lErr" class="err-msg"></div>
    </form>
    <form id="regForm" style="display:none">
      <h2 style="text-align:center;margin-bottom:16px;">Register</h2>
      <div class="form-group"><label>Username</label><input id="ru" type="text" required></div>
      <div class="form-group"><label>Email</label><input id="re" type="email" required></div>
      <div class="form-group"><label>Full Name</label><input id="rn" type="text" required></div>
      <div class="form-group"><label>Set Password</label><div class="pw-wrap"><input id="rp" type="password" required minlength="6" placeholder="Min. 6 characters"><button type="button" class="pw-eye" onclick="togglePw('rp',this)" title="Show/hide password">👁️</button></div></div>
      <div class="form-group"><label>Confirm Password</label><div class="pw-wrap"><input id="rpc" type="password" required placeholder="Re-enter password"><button type="button" class="pw-eye" onclick="togglePw('rpc',this)" title="Show/hide password">👁️</button></div></div>
      <div class="form-group"><label>Department</label>
        <select id="rd" required><option value="">Select</option><option>ECE</option><option>CSE</option><option>EIE</option><option>EEE</option><option>ICT</option></select></div>
      <button type="submit" class="btn">Register</button>
      <div style="text-align:center;margin-top:10px;font-size:.88em">Have account? <a href="#" onclick="showLogin()">Login</a></div>
      <div id="rErr" class="err-msg"></div>
    </form>
  </div>

  <!-- MAIN -->
  <div id="mainSection" style="display:none">
    <div class="user-bar">
      <div style="display:flex;gap:14px;align-items:center">
        <div class="avatar" id="avt">U</div>
        <div><div style="font-weight:700;font-size:1.05em" id="uName">User</div>
             <div style="color:#666;font-size:.86em" id="uRole">Role</div></div>
      </div>
      <div>
        <button class="notif-btn" id="notifToggleBtn" onclick="toggleNotifications()">🔔 Enable Alerts</button>
        <button class="logout-btn" onclick="logout()">Logout</button>
      </div>
    </div>

    <!-- Non-admin nav -->
    <div class="nav-tabs" id="navTabs" style="display:none">
      <button class="nav-tab active" onclick="showTab('book')">📋 Book Lab</button>
      <button class="nav-tab" onclick="showTab('cal')">📅 Calendar</button>
      <button class="nav-tab" onclick="showTab('mine')">📂 My Bookings</button>
      <button class="nav-tab" id="chatTabBtn" onclick="showTab('chat')">🤖 AI Chatbot</button>
    </div>

    <!-- ADMIN PANEL -->
    <div id="adminPanel" style="display:none">
      <div class="retrain-info">🔄 <b>ML Status:</b> <span id="mlStatsTxt">Loading…</span>
        <button onclick="manualRetrain()" style="margin-left:10px;padding:4px 10px;background:#28a745;color:white;border:none;border-radius:5px;cursor:pointer;font-size:.8em">↺ Retrain Now</button>
      </div>
      <div class="card">
        <div class="card-title">👨‍💼 Admin Dashboard
          <button onclick="showQrVerifier()" style="margin-left:auto;padding:6px 14px;background:#667eea;color:white;border:none;border-radius:7px;cursor:pointer;font-size:.82em;font-weight:600">📱 Verify QR</button>
        </div>
        <div class="stats-bar" id="statsBar"></div>
        <div class="filter-btns">
          <button class="filter-btn active" onclick="filterAdmin('pending')" id="fp">Pending (<span id="pc">0</span>)</button>
          <button class="filter-btn" onclick="filterAdmin('approved')" id="fa">Approved (<span id="ac">0</span>)</button>
          <button class="filter-btn" onclick="filterAdmin('rejected')" id="fr">Rejected (<span id="rc">0</span>)</button>
          <button class="filter-btn" onclick="filterAdmin('all')" id="fall">All</button>
        </div>
        <div id="adminList"></div>
      </div>
      <div class="card">
        <div class="card-title">📅 Lab Calendar</div>
        <div class="cal-controls">
          <select id="acLab" onchange="renderCal('admin')">__LAB_OPTIONS__</select>
          <button class="cal-nav" onclick="calShift(-7,'admin')">◀ Prev</button>
          <span id="aCalLbl" style="font-weight:700"></span>
          <button class="cal-nav" onclick="calShift(7,'admin')">Next ▶</button>
        </div>
        <div style="overflow-x:auto" id="aCalGrid"></div>
      </div>
    </div>

    <!-- BOOK TAB -->
    <div id="tabBook">
      <div class="card"><div class="card-title">🏫 Select Lab</div><div class="lab-grid" id="labGrid"></div></div>
      <div class="card"><div class="card-title">📅 Select Date</div>
        <input type="date" id="datePick" style="padding:10px 12px;border:2px solid #ddd;border-radius:8px;width:220px;font-size:.95em" onchange="dateChanged()">
      </div>
      <div class="card" id="slotCard" style="display:none">
        <div class="card-title">🕐 Time Slot <small style="margin-left:auto;font-size:.72em;color:#666">🔴 Blocked · Demand: 🔴H 🟠M 🟢L</small></div>
        <div class="slots-grid" id="slotsGrid"></div>
      </div>
      <div class="card" id="seatCard" style="display:none">
        <div class="card-title">💻 Seat Map <small style="margin-left:auto;font-size:.7em;color:#666">Live real-time locking</small></div>
        <div class="seat-legend">
          <div class="legend-item"><div class="legend-dot" style="background:#d4edda"></div> Free</div>
          <div class="legend-item"><div class="legend-dot" style="background:#667eea"></div> Selected</div>
          <div class="legend-item"><div class="legend-dot" style="background:#f8d7da"></div> 🔒 Locked</div>
          <div class="legend-item"><div class="legend-dot" style="background:#fff3cd"></div> 📌 Your booking</div>
        </div>
        <div class="seat-map" id="seatMap"></div>
      </div>
      <div class="card" id="purposeCard" style="display:none">
        <div class="card-title">📝 Purpose &amp; AI Analysis</div>
        <textarea id="purpose" rows="3" style="width:100%;padding:10px;border:2px solid #ddd;border-radius:8px;font-size:.95em"
          placeholder="Describe purpose (more detail → higher ML confidence)…" oninput="debouncedML()"></textarea>
        <div class="ml-card" id="mlCard">
          <div class="ml-title">🤖 Real-time AI Analysis</div>
          <div class="ml-grid" id="mlGrid"></div>
          <div class="ml-reason" id="mlReason"></div>
          <div class="countdown" id="approvalCountdown"></div>
        </div>
        <div class="waitlist-form" id="waitlistForm" style="display:none">
          <p style="font-weight:600;margin-bottom:8px">📋 Join Waitlist</p>
          <p style="font-size:.85em;color:#666;margin-bottom:10px">All seats taken. Get notified when a slot opens.</p>
          <button class="btn" onclick="joinWaitlist()">Join Waitlist</button>
        </div>
        <button id="submitBtn" class="btn" onclick="submitBooking()" disabled style="margin-top:13px">Submit Booking</button>
        <div id="successMsg" class="success-msg"></div>
      </div>
    </div>

    <!-- CALENDAR TAB -->
    <div id="tabCal" style="display:none">
      <div class="card"><div class="card-title">📅 Weekly Lab Calendar</div>
        <div class="cal-controls">
          <select id="ucLab" onchange="renderCal('user')">__LAB_OPTIONS__</select>
          <button class="cal-nav" onclick="calShift(-7,'user')">◀ Prev</button>
          <span id="uCalLbl" style="font-weight:700"></span>
          <button class="cal-nav" onclick="calShift(7,'user')">Next ▶</button>
        </div>
        <div class="cal-legend">
          <span class="cal-tt" style="padding:2px 8px;border-radius:4px">🔴 Class</span>
          <span class="cal-approved" style="padding:2px 8px;border-radius:4px">🟢 Approved</span>
          <span class="cal-pending" style="padding:2px 8px;border-radius:4px">🟡 Pending</span>
          <span class="cal-free" style="padding:2px 8px;border-radius:4px">✅ Free</span>
        </div>
        <div style="overflow-x:auto" id="uCalGrid"></div>
      </div>
    </div>

    <!-- MY BOOKINGS TAB -->
    <div id="tabMine" style="display:none">
      <div class="card"><div class="card-title">📂 My Bookings</div><div id="myList"></div></div>
    </div>

    <!-- CHATBOT TAB -->
    <div id="tabChat" style="display:none">
      <div class="card">
        <div class="card-title">🤖 Lab Assistant AI Chatbot
          <span id="llmStatusBadge" style="margin-left:auto;font-size:.72em;padding:3px 9px;border-radius:12px;font-weight:600;background:#fff3cd;color:#856404">Checking…</span>
        </div>
        <div id="llmOfflineMsg" style="display:none;background:#fff3cd;border:2px solid #ffc107;border-radius:9px;padding:14px;margin-bottom:14px;font-size:.88em;color:#856404">
          ⚠️ <b>LLM model not loaded.</b> The chatbot requires trained model files.<br><br>
          To enable it:<br>
          1. Run <code style="background:#f8f9fa;padding:2px 6px;border-radius:4px">python train_lab_llm.py --mode train</code><br>
          2. This creates <code style="background:#f8f9fa;padding:2px 6px;border-radius:4px">checkpoints/vocab.pkl</code> and <code style="background:#f8f9fa;padding:2px 6px;border-radius:4px">checkpoints/best.pt</code><br>
          3. Restart the server — the chatbot will load automatically.
        </div>
        <div id="llmOnlineMsg" style="display:none;background:#d4edda;border:1px solid #c3e6cb;border-radius:9px;padding:10px 14px;margin-bottom:14px;font-size:.84em;color:#155724">
          ✅ <b>GPT-style LLM loaded</b> — Ask me about lab timetables, course schedules, faculty assignments, or availability!
        </div>
        <div id="chatMessages" style="min-height:200px;max-height:400px;overflow-y:auto;border:2px solid #e0e0e0;border-radius:10px;padding:14px;margin-bottom:12px;background:white">
          <div style="text-align:center;color:#aaa;padding:40px 20px;font-size:.9em">💬 Ask a question to get started…</div>
        </div>
        <div style="display:flex;gap:8px">
          <input id="chatInput" type="text" placeholder="e.g. What labs are free on Monday at 9am?"
            style="flex:1;padding:10px 12px;border:2px solid #ddd;border-radius:8px;font-size:.95em"
            onkeydown="if(event.key==='Enter')sendChat()">
          <button onclick="sendChat()" class="btn" style="width:auto;padding:10px 20px">Ask</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Forgot Password modal -->
  <div id="forgotModal" class="modal">
    <div class="modal-box">
      <div style="font-size:1.3em;font-weight:700;margin-bottom:14px;color:#667eea">🔑 Forgot Password</div>
      <!-- Step 1: Enter username -->
      <div id="fpStep1">
        <p style="font-size:.88em;color:#666;margin-bottom:14px">Enter your <b>username</b>. We will send a reset link to the email registered with that account.</p>
        <div class="form-group"><label>Username</label><input id="fpUsername" type="text" placeholder="Your username" autocomplete="username"></div>
        <div id="fpErr" class="err-msg"></div>
        <button class="btn" onclick="submitForgotPassword()" style="margin-top:10px" id="fpSendBtn">📧 Send Reset Link</button>
        <button class="btn" onclick="closeModal('forgotModal')" style="margin-top:8px;background:#6c757d">Cancel</button>
      </div>
      <!-- Step 2: Email sent confirmation -->
      <div id="fpStep2" style="display:none;text-align:center;padding:10px 0">
        <div style="font-size:3em;margin-bottom:12px">📬</div>
        <div style="font-weight:700;font-size:1.05em;color:#28a745;margin-bottom:8px">Reset link sent!</div>
        <p style="font-size:.88em;color:#555;margin-bottom:18px" id="fpStep2Msg">Check your inbox and click the link to set a new password. The link expires in 30 minutes.</p>
        <button class="btn" onclick="closeModal('forgotModal')" style="background:#667eea">Close</button>
      </div>
    </div>
  </div>

  <!-- Reset Password inline page (shown when arriving via email link) -->
  <div id="resetPasswordPage" style="display:none">
    <div class="auth-box" style="max-width:440px">
      <h2 style="text-align:center;color:#667eea;margin-bottom:6px;">🔒 Set New Password</h2>
      <p style="text-align:center;font-size:.88em;color:#666;margin-bottom:20px;">Enter and confirm your new password below.</p>
      <div class="form-group"><label>New Password</label><div class="pw-wrap"><input id="rpNewPass" type="password" placeholder="New password (min 6 chars)"><button type="button" class="pw-eye" onclick="togglePw('rpNewPass',this)" title="Show/hide password">👁️</button></div></div>
      <div class="form-group"><label>Confirm New Password</label><div class="pw-wrap"><input id="rpNewPassC" type="password" placeholder="Confirm new password"><button type="button" class="pw-eye" onclick="togglePw('rpNewPassC',this)" title="Show/hide password">👁️</button></div></div>
      <div id="rpErr" class="err-msg"></div>
      <div id="rpSuccess" style="display:none;background:#d4edda;color:#155724;padding:12px;border-radius:8px;margin-bottom:10px;">✅ Password changed! You can now <a href="/" style="color:#155724;font-weight:700">login</a>.</div>
      <button class="btn" onclick="submitNewPassword()" id="rpBtn">Set New Password</button>
    </div>
  </div>

  <!-- Class modal -->
  <div id="classModal" class="modal">
    <div class="modal-box">
      <div style="font-size:1.3em;font-weight:700;margin-bottom:14px;color:#dc3545">🚫 Blocked – Class Schedule</div>
      <div id="modalInfo"></div>
      <button class="btn" onclick="closeModal('classModal')" style="margin-top:14px">Close</button>
    </div>
  </div>

  <!-- QR modal -->
  <div id="qrModal" class="modal">
    <div class="modal-box" style="text-align:center">
      <div style="font-size:1.3em;font-weight:700;margin-bottom:4px;color:#667eea">📱 Booking QR Code</div>
      <div id="qrModalContent"></div>
      <button class="btn" onclick="closeModal('qrModal')" style="margin-top:14px;max-width:200px">Close</button>
    </div>
  </div>

  <!-- QR Verify modal (admin) -->
  <div id="qrVerifyModal" class="modal">
    <div class="modal-box">
      <div style="font-size:1.3em;font-weight:700;margin-bottom:14px;color:#667eea">📱 Verify Booking QR</div>
      <p style="font-size:.88em;color:#666;margin-bottom:12px">Paste the text from the scanned QR code below to verify a booking:</p>
      <textarea id="qrVerifyInput" rows="3" style="width:100%;padding:10px;border:2px solid #ddd;border-radius:8px;font-size:.88em" placeholder="SASTRA|MEM001234...|dsp1|2025-06-10|..."></textarea>
      <button class="btn" onclick="verifyQrCode()" style="margin-top:10px">✅ Verify</button>
      <div id="qrVerifyResult" style="margin-top:12px;display:none"></div>
      <button class="btn" onclick="closeModal('qrVerifyModal')" style="margin-top:10px;background:#6c757d">Close</button>
    </div>
  </div>
</div>

<script>
const TT   = __TIMETABLE__;
const LABS = __LABS__;
const SLOTS= __SLOTS__;

let cu=null, adminFilter='pending';
let selLab=null, selDate=null, selTime=null;
let selPCs=new Set(), demandMap={};
let mlTimer=null, sseSource=null;
const calOff={admin:0,user:0};

/* ── Toast ─────────────────────────────────────────────────────────────────── */
function toast(msg,type='info',dur=4000){
  const t=document.createElement('div');
  t.className=`toast ${type}`;t.innerHTML=msg;
  t.onclick=()=>t.remove();
  document.getElementById('toastContainer').appendChild(t);
  setTimeout(()=>{if(t.parentNode)t.remove()},dur);
}

/* ── Browser Notifications ─────────────────────────────────────────────────── */
let notifEnabled=false;

function checkNotifSupport(){
  if(!('Notification' in window)) return false;
  if(Notification.permission==='granted'){notifEnabled=true;updateNotifBtn();return true}
  if(Notification.permission!=='denied'){
    document.getElementById('notifBanner').classList.add('show');
  }
  return false;
}

function updateNotifBtn(){
  const btn=document.getElementById('notifToggleBtn');
  if(!btn) return;
  if(notifEnabled){btn.textContent='🔔 Alerts ON';btn.style.background='#28a745'}
  else{btn.textContent='🔔 Enable Alerts';btn.style.background='#667eea'}
}

async function requestNotifPermission(){
  if(!('Notification' in window)){toast('❌ Browser does not support notifications','error');return}
  const perm=await Notification.requestPermission();
  if(perm==='granted'){
    notifEnabled=true;updateNotifBtn();
    document.getElementById('notifBanner').classList.remove('show');
    toast('🔔 Notifications enabled!','success');
    new Notification('SASTRA Lab Booking',{body:'You will now receive instant approval alerts!',icon:'/favicon.ico'});
    startSSE();
  } else {
    toast('⚠️ Notification permission denied. You can enable it in browser settings.','warning');
  }
}

function toggleNotifications(){
  if(notifEnabled){
    notifEnabled=false;updateNotifBtn();
    toast('🔕 Notifications disabled','info');
    if(sseSource){sseSource.close();sseSource=null;}
  } else {
    requestNotifPermission();
  }
}

function dismissNotifBanner(){document.getElementById('notifBanner').classList.remove('show')}

function pushNotification(title, body, icon='🔔'){
  // In-app toast always
  toast(`<b>${title}</b><br><small>${body}</small>`,'info',5000);
  // OS notification if permitted
  if(notifEnabled && Notification.permission==='granted'){
    try{new Notification(title,{body,icon:'/favicon.ico'})}catch(e){}
  }
}

/* ── SSE (Server-Sent Events) stream ──────────────────────────────────────── */
function startSSE(){
  if(sseSource) sseSource.close();
  sseSource=new EventSource('/api/notify-stream');
  sseSource.onmessage=e=>{
    try{
      const d=JSON.parse(e.data);
      if(d.type==='heartbeat') return;
      if(d.type==='approved'){
        pushNotification('✅ Booking Approved!',d.data.message||'Your booking was approved.');
        // Refresh my bookings if open
        if(document.getElementById('tabMine').style.display!=='none') loadMyBookings();
      } else if(d.type==='rejected'){
        pushNotification('❌ Booking Rejected',d.data.message||'Your booking was rejected.');
        if(document.getElementById('tabMine').style.display!=='none') loadMyBookings();
      } else if(d.type==='reminder'){
        pushNotification('⏰ Session Reminder!',d.data.message||'Your lab session starts in 30 minutes.');
      } else if(d.type==='waitlist'){
        pushNotification('🎉 Waitlist Slot!',d.data.message||'A slot is now available.');
      }
    }catch(err){}
  };
  sseSource.onerror=()=>{sseSource.close();sseSource=null;setTimeout(()=>{if(notifEnabled)startSSE()},10000)};
}

/* ── Auth ──────────────────────────────────────────────────────────────────── */
function showReg(){document.getElementById('loginForm').style.display='none';document.getElementById('regForm').style.display='block'}
function showLogin(){document.getElementById('regForm').style.display='none';document.getElementById('loginForm').style.display='block'}
async function googleLogin(){
  try{
    const r=await fetch('/auth/google');
    const d=await r.json();
    if(d.auth_url) window.location.href=d.auth_url;
    else toast('❌ Google login failed','error');
  }catch(e){toast('❌ Network error','error')}
}
document.getElementById('loginForm').addEventListener('submit',async e=>{
  e.preventDefault();
  const btn=document.getElementById('loginBtn');btn.disabled=true;btn.textContent='Logging in…';
  document.getElementById('lErr').style.display='none';
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:document.getElementById('lu').value.trim(),password:document.getElementById('lp').value})});
    const d=await r.json();
    if(d.success){cu=d.user;initMain()}
    else{const e=document.getElementById('lErr');e.textContent='❌ '+(d.message||'Login failed');e.style.display='block'}
  }catch(err){const e=document.getElementById('lErr');e.textContent='❌ Network error';e.style.display='block'}
  finally{btn.disabled=false;btn.textContent='Login'}
});

document.getElementById('regForm').addEventListener('submit',async e=>{
  e.preventDefault();
  const em=document.getElementById('re').value.trim();
  const pw=document.getElementById('rp').value;
  const pwc=document.getElementById('rpc')?document.getElementById('rpc').value:pw;
  if(pw.length<6){const er=document.getElementById('rErr');er.textContent='❌ Password must be at least 6 characters';er.style.display='block';return}
  if(pw!==pwc){const er=document.getElementById('rErr');er.textContent='❌ Passwords do not match';er.style.display='block';return}
  try{
    const r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:document.getElementById('ru').value.trim(),email:em,
        name:document.getElementById('rn').value.trim(),password:document.getElementById('rp').value,
        department:document.getElementById('rd').value,google_auth:false})});
    const d=await r.json();
    if(d.success){toast('✅ Registered! Please login.','success');showLogin()}
    else{const e=document.getElementById('rErr');e.textContent='❌ '+(d.message||'Error');e.style.display='block'}
  }catch(err){const e=document.getElementById('rErr');e.textContent='❌ Network error';e.style.display='block'}
});

function logout(){
  if(sseSource){sseSource.close();sseSource=null;}
  fetch('/api/logout',{method:'POST'}).then(()=>location.reload());
}

/* ── Init main ─────────────────────────────────────────────────────────────── */
function initMain(){
  document.getElementById('authSection').style.display='none';
  document.getElementById('mainSection').style.display='block';
  document.getElementById('uName').textContent=cu.name;
  document.getElementById('uRole').textContent=cu.role.toUpperCase();
  document.getElementById('avt').textContent=cu.name.charAt(0).toUpperCase();

  // Build lab grid
  const lg=document.getElementById('labGrid');lg.innerHTML='';
  Object.entries(LABS).forEach(([lid,li])=>{
    const div=document.createElement('div');div.className='lab-card';
    div.innerHTML=`<div style="font-size:2.2em;margin-bottom:7px">${li.icon}</div>
      <div style="font-weight:700;font-size:1.05em">${li.name}</div>
      <div style="font-size:.83em;opacity:.8">${li.floor} · ${li.computers} PCs</div>`;
    div.onclick=()=>selectLab(lid,div); lg.appendChild(div);
  });

  checkNotifSupport();
  startSSE(); // always open SSE stream (for approved notifications even without OS permission)

  if(cu.role==='admin'){
    document.getElementById('adminPanel').style.display='block';
    loadAdminBookings('pending');loadMLStats();renderCal('admin');
    setInterval(()=>loadAdminBookings(adminFilter),8000);
  } else {
    document.getElementById('navTabs').style.display='flex';
    document.getElementById('tabBook').style.display='block';
    document.getElementById('datePick').min=new Date().toISOString().split('T')[0];
    checkLLMStatus();
  }
}

/* ── Tabs ──────────────────────────────────────────────────────────────────── */
function showTab(t){
  document.getElementById('tabBook').style.display=t==='book'?'block':'none';
  document.getElementById('tabCal').style.display=t==='cal'?'block':'none';
  document.getElementById('tabMine').style.display=t==='mine'?'block':'none';
  document.getElementById('tabChat').style.display=t==='chat'?'block':'none';
  document.querySelectorAll('.nav-tab').forEach((b,i)=>{b.classList.toggle('active',['book','cal','mine','chat'][i]===t)});
  if(t==='cal')renderCal('user');
  if(t==='mine')loadMyBookings();
}

/* ── ML Stats ──────────────────────────────────────────────────────────────── */
async function loadMLStats(){
  try{const r=await fetch('/api/ml-stats');const d=await r.json();
    document.getElementById('mlStatsTxt').textContent=
      d.trained?`XGBoost ${(d.xgb_accuracy*100).toFixed(1)}% · ${d.data_source} · 12 features`:'Not trained';}catch(e){}
}
async function manualRetrain(){toast('🔄 Retraining…','info');await fetch('/api/retrain-ml',{method:'POST'});setTimeout(loadMLStats,5000)}

/* ── Lab/Date/Slot ─────────────────────────────────────────────────────────── */
function selectLab(lid,el){selLab=lid;document.querySelectorAll('.lab-card').forEach(c=>c.classList.remove('selected'));el.classList.add('selected');if(selDate)fetchDemandAndSlots()}
function dateChanged(){selDate=document.getElementById('datePick').value;if(selLab&&selDate)fetchDemandAndSlots()}
async function fetchDemandAndSlots(){
  try{const r=await fetch('/api/demand-forecast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lab:selLab,date:selDate})});demandMap=(await r.json()).forecast||{};}catch(e){demandMap={}}
  renderSlots();
}
function renderSlots(){
  const c=document.getElementById('slotsGrid');c.innerHTML='';
  const day=dayName(selDate); const tt=(TT[selLab]&&TT[selLab][day])||{};
  SLOTS.forEach(slot=>{
    const div=document.createElement('div');div.className='slot';
    const dem=demandMap[slot]||'Low';
    if(tt[slot]){div.classList.add('blocked');div.innerHTML=slot+' 🚫';div.onclick=()=>showClassInfo(slot,tt[slot],day)}
    else{div.classList.add('free');div.innerHTML=`${slot} ✓<span class="demand-tag d${dem}">Demand: ${dem}</span>`;div.onclick=()=>selectSlot(slot,div)}
    c.appendChild(div);
  });
  document.getElementById('slotCard').style.display='block';
}
function selectSlot(slot,el){
  selTime=slot;selPCs.clear();
  document.querySelectorAll('.slot').forEach(s=>s.classList.remove('selected'));el.classList.add('selected');
  renderSeatMap();
  document.getElementById('seatCard').style.display='block';document.getElementById('purposeCard').style.display='block';
  document.getElementById('waitlistForm').style.display='none';document.getElementById('submitBtn').style.display='block';
  debouncedML();
}
function showClassInfo(slot,info,day){
  document.getElementById('modalInfo').innerHTML=`
    <div class="info-row"><span style="font-weight:600">📅 Day</span><span>${day}</span></div>
    <div class="info-row"><span style="font-weight:600">🕐 Time</span><span>${slot}</span></div>
    <div class="info-row"><span style="font-weight:600">📚 Course</span><span>${info.course}</span></div>
    <div class="info-row"><span style="font-weight:600">📖 Subject</span><span>${info.name}</span></div>
    ${info.section?`<div class="info-row"><span style="font-weight:600">👥 Section</span><span>${info.section}</span></div>`:''}
    <div class="info-row"><span style="font-weight:600">👨‍🏫 Faculty</span><span>${info.faculty||'—'}</span></div>`;
  document.getElementById('classModal').classList.add('show');
}
function closeModal(id){document.getElementById(id).classList.remove('show')}

/* ── Seat Map ──────────────────────────────────────────────────────────────── */
async function renderSeatMap(){
  const c=document.getElementById('seatMap');
  c.innerHTML='<p style="text-align:center;padding:20px;color:#666">⏳ Loading…</p>';
  let data={available:[],booked:[],booked_details:[]};
  try{const r=await fetch('/api/available-computers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lab:selLab,date:selDate,time:selTime})});data=await r.json();}catch(e){}
  const lockedMap={};
  (data.booked_details||[]).forEach(b=>{(b.pcs||[]).forEach(pc=>{lockedMap[pc]={name:b.userName,isMe:b.isMe,status:b.status}})});
  c.innerHTML='';let allLocked=true;
  for(let i=1;i<=33;i++){
    const seat=document.createElement('div');seat.className='seat';
    if(lockedMap[i]){
      const info=lockedMap[i];seat.classList.add(info.isMe?'my-booked':'locked');
      seat.innerHTML=`<span class="seat-icon">${info.isMe?'📌':'🔒'}</span><span class="seat-num">${i}</span><span class="seat-owner">${info.isMe?'Mine':(info.name||'').split(' ')[0]||'Booked'}</span>`;
      seat.title=`${info.isMe?'Your booking':'Booked by '+info.name} (${info.status})`;
    }else{allLocked=false;seat.classList.add('free');seat.innerHTML=`<span class="seat-icon">💻</span><span class="seat-num">${i}</span>`;seat.onclick=()=>toggleSeat(i,seat)}
    c.appendChild(seat);
  }
  document.getElementById('waitlistForm').style.display=allLocked?'block':'none';
  document.getElementById('submitBtn').style.display=allLocked?'none':'block';
}
function toggleSeat(n,el){
  if(selPCs.has(n)){selPCs.delete(n);el.classList.remove('selected');el.classList.add('free')}
  else{selPCs.add(n);el.classList.remove('free');el.classList.add('selected')}
  updateSubmit();debouncedML();
}
function updateSubmit(){document.getElementById('submitBtn').disabled=!(selLab&&selDate&&selTime&&selPCs.size>0)}

/* ── ML Preview ───────────────────────────────────────────────────────────── */
function debouncedML(){clearTimeout(mlTimer);mlTimer=setTimeout(fetchML,500)}
async function fetchML(){
  if(!selLab||!selDate||!selTime) return;
  try{const r=await fetch('/api/ml-preview',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({lab:selLab,date:selDate,time:selTime,num_computers:selPCs.size||1,purpose:document.getElementById('purpose').value.trim()})});
    const d=await r.json();renderMLCard(d);}catch(e){}
}
function renderMLCard(d){
  const card=document.getElementById('mlCard');
  if(!d||d.xgb_confidence==null){card.classList.remove('show');return}
  card.classList.add('show');
  const col=d.ml_status==='approved'?'#38ef7d':d.ml_status==='rejected'?'#ff6b6b':'#f9ca24';
  document.getElementById('mlGrid').innerHTML=`
    <div class="ml-box"><div class="ml-val" style="color:${col}">${Math.round(d.xgb_confidence*100)}%</div><div class="ml-lbl">XGBoost Conf.</div></div>
    <div class="ml-box"><div class="ml-val" style="color:${d.is_anomaly?'#ff6b6b':'#38ef7d'}">${d.is_anomaly?'⚠️ Yes':'✅ No'}</div><div class="ml-lbl">Anomaly</div></div>
    <div class="ml-box"><div class="ml-val" style="color:#a29bfe">${d.demand_level||'N/A'}</div><div class="ml-lbl">Demand</div></div>
    <div class="ml-box"><div class="ml-val" style="color:${col}">${(d.ml_status||'pending').toUpperCase()}</div><div class="ml-lbl">Predicted</div></div>`;
  document.getElementById('mlReason').textContent='🤖 '+(d.ml_reason||'');
  document.getElementById('approvalCountdown').textContent=
    (d.recommendation==='auto_approve')?'⏳ Will auto-approve in ~5 minutes':'⏳ Requires admin review';
}

/* ── Submit ───────────────────────────────────────────────────────────────── */
const _clientRate=[];
function checkRateClient(){
  const now=Date.now();const valid=_clientRate.filter(t=>now-t<3600000);
  if(valid.length>=10)return false;valid.push(now);_clientRate.splice(0,_clientRate.length,...valid);return true;
}
async function submitBooking(){
  const purpose=document.getElementById('purpose').value.trim();
  if(!purpose){toast('⚠️ Please enter a purpose','warning');return}
  if(!checkRateClient()){toast('⚠️ Too many bookings this hour','warning');return}
  const btn=document.getElementById('submitBtn');btn.disabled=true;btn.textContent='Submitting…';
  try{
    const r=await fetch('/api/book',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({lab:selLab,date:selDate,time:selTime,computers:Array.from(selPCs),purpose})});
    const d=await r.json();
    if(d.success){
      const msg=document.getElementById('successMsg');
      msg.innerHTML=`✅ Booking submitted! ID: ${(d.bookingId||'').substring(0,12)}<br><small>📧 Confirmation email sent · 🔔 You'll get notified on approval</small>`;
      msg.classList.add('show');toast('✅ Booking submitted!','success');
      setTimeout(()=>msg.classList.remove('show'),6000);
      selPCs.clear();renderSeatMap();updateSubmit();document.getElementById('mlCard').classList.remove('show');
    }else toast('❌ '+d.message,'error');
  }catch(err){toast('❌ Network error','error')}
  finally{btn.disabled=false;btn.textContent='Submit Booking';updateSubmit()}
}
async function joinWaitlist(){
  if(!selLab||!selDate||!selTime){toast('Select lab, date and time first','warning');return}
  try{const r=await fetch('/api/waitlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lab:selLab,date:selDate,time:selTime})});
    const d=await r.json();
    if(d.success)toast("📋 Added to waitlist! You'll be notified when a slot opens.",'success');
    else toast('❌ '+d.message,'error');
  }catch(e){toast('❌ Network error','error')}
}

/* ── QR Code ──────────────────────────────────────────────────────────────── */
async function showQr(bookingId){
  try{
    const r=await fetch('/api/qr/'+bookingId);const d=await r.json();
    if(!d.qr_image){toast('❌ QR not available for this booking','error');return}
    const pcs=(d.computers||[]).join(', ')||'N/A';
    document.getElementById('qrModalContent').innerHTML=`
      <div style="margin-bottom:12px">
        <div style="font-size:.95em;font-weight:600;color:#333;margin-bottom:4px">${d.userName||''}</div>
        <div style="color:#666;font-size:.85em">${d.lab} &nbsp;·&nbsp; ${d.date} &nbsp;·&nbsp; ${d.time}</div>
        <div style="color:#667eea;font-size:.82em;margin-top:2px">💻 Computers: ${pcs}</div>
      </div>
      <div style="display:inline-block;padding:14px;border:3px solid #667eea;border-radius:12px;background:white">
        <img src="${d.qr_image}" style="display:block;width:220px;height:220px;image-rendering:pixelated" alt="QR Code">
      </div>
      <p style="font-size:.78em;color:#888;margin-top:10px">📱 Scan with any QR reader to verify this booking.</p>
      <details style="margin-top:10px;font-size:.75em;color:#666;text-align:left">
        <summary style="cursor:pointer;color:#667eea">Show encoded data</summary>
        <pre style="margin-top:6px;padding:8px;background:#f8f9fa;border-radius:6px;white-space:pre-wrap;word-break:break-all">${d.qr_text}</pre>
      </details>`;
    document.getElementById('qrModal').classList.add('show');
  }catch(e){toast('❌ Could not load QR','error')}
}

function showQrVerifier(){document.getElementById('qrVerifyInput').value='';document.getElementById('qrVerifyResult').style.display='none';document.getElementById('qrVerifyModal').classList.add('show')}
async function verifyQrCode(){
  const text=document.getElementById('qrVerifyInput').value.trim();
  if(!text){toast('Paste QR text first','warning');return}
  try{
    const r=await fetch('/api/qr-verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({qr_text:text})});
    const d=await r.json();
    const res=document.getElementById('qrVerifyResult');res.style.display='block';
    if(d.valid){
      res.innerHTML=`<div style="background:#d4edda;border:2px solid #28a745;border-radius:8px;padding:12px">
        <b style="color:#155724">✅ VALID BOOKING</b><br>
        <small>👤 ${d.booking.userName} · 🏫 ${LABS[d.booking.lab]?.name||d.booking.lab}<br>
        📅 ${d.booking.date} · 🕐 ${d.booking.time}<br>
        💻 PCs: ${(d.booking.computers||[]).join(', ')}<br>
        📊 Status: <b>${d.booking.status?.toUpperCase()}</b></small></div>`;
    }else{
      res.innerHTML=`<div style="background:#f8d7da;border:2px solid #dc3545;border-radius:8px;padding:12px"><b style="color:#721c24">❌ ${d.message||'Invalid QR code'}</b></div>`;
    }
  }catch(e){toast('❌ Verification error','error')}
}

/* ── My Bookings ──────────────────────────────────────────────────────────── */
async function loadMyBookings(){
  try{const r=await fetch('/api/my-bookings');const d=await r.json();
    const c=document.getElementById('myList');
    if(!d.bookings||!d.bookings.length){c.innerHTML='<p style="text-align:center;color:#666;padding:20px">No bookings yet</p>';return}
    c.innerHTML=d.bookings.map(b=>buildCard(b,false)).join('');}catch(e){}
}

/* ── Admin ──────────────────────────────────────────────────────────────────── */
function filterAdmin(s){adminFilter=s;document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));const ids={pending:'fp',approved:'fa',rejected:'fr',all:'fall'};document.getElementById(ids[s])?.classList.add('active');loadAdminBookings(s)}
async function loadAdminBookings(status='all'){
  try{const r=await fetch('/api/all-bookings?status='+status);const d=await r.json();
    if(d.counts){
      document.getElementById('pc').textContent=d.counts.pending;
      document.getElementById('ac').textContent=d.counts.approved;
      document.getElementById('rc').textContent=d.counts.rejected;
      document.getElementById('statsBar').innerHTML=`
        <div class="stat"><div class="stat-val">${d.counts.total||0}</div><div class="stat-lbl">Total</div></div>
        <div class="stat"><div class="stat-val">${d.counts.pending}</div><div class="stat-lbl">Pending</div></div>
        <div class="stat"><div class="stat-val">${d.counts.approved}</div><div class="stat-lbl">Approved</div></div>
        <div class="stat"><div class="stat-val">${d.counts.rejected}</div><div class="stat-lbl">Rejected</div></div>`;
    }
    const c=document.getElementById('adminList');
    if(!d.bookings||!d.bookings.length){c.innerHTML=`<p style="text-align:center;color:#666;padding:20px">No ${status} bookings</p>`;return}
    c.innerHTML=d.bookings.map(b=>buildCard(b,true)).join('');}catch(e){}
}

function buildCard(b,isAdmin){
  const labName=LABS[b.lab]?.name||b.lab||'Unknown';
  const pcs=(b.computers||[]).join(', ')||'N/A';
  const st=b.status||'pending';
  const conf=b.ml_confidence!=null?Math.round(b.ml_confidence*100)+'%':'N/A';
  const anom=b.is_anomaly?'<span style="color:#dc3545;font-weight:700">⚠️ Anomaly</span>':'✅ Normal';
  const mlBlock=`<div class="ml-mini">🤖 Conf: <b>${conf}</b> · Demand: <b>${b.ml_demand_level||'N/A'}</b> · ${anom}${b.ml_reason?'<br><small>'+b.ml_reason+'</small>':''}</div>`;
  const cancelBtn=(!isAdmin&&(st==='pending'||st==='approved'))?`<button class="act-btn cancel-btn" onclick="cancelBooking('${b.id}')">🚫 Cancel</button>`:'';
  const adminAct=isAdmin&&st==='pending'?`<button class="act-btn approve-btn" onclick="approveBooking('${b.id}')">✅ Approve</button><button class="act-btn reject-btn" onclick="rejectBooking('${b.id}')">❌ Reject</button>`:'';
  const noshowBtn=isAdmin&&st==='approved'?`<button class="act-btn noshow-btn" onclick="markNoShow('${b.id}')">❌ No-Show</button>`:'';
  // QR button — shown for approved bookings
  const qrBtn=st==='approved'?`<button class="act-btn qr-btn" onclick="showQr('${b.id}')">📱 QR</button>`:'';
  const appInfo=st==='approved'?`<div style="color:#28a745;margin-top:7px;font-size:.82em">✅ Approved by: ${b.approvedBy||'System'}</div>`:'';
  const rejInfo=st==='rejected'?`<div style="color:#dc3545;margin-top:7px;font-size:.82em">❌ Rejected: ${b.rejectionReason||'N/A'}</div>`:'';
  return `<div class="booking-card ${st}">
    <div class="booking-head"><b>${isAdmin?(b.userName||'User'):labName}</b><span class="status-badge ${st}">${st.toUpperCase()}</span></div>
    ${isAdmin?`<div class="booking-row"><span class="booking-lbl">Email:</span>${b.userEmail||'N/A'}</div>`:''}
    <div class="booking-row"><span class="booking-lbl">Lab:</span>${labName}</div>
    <div class="booking-row"><span class="booking-lbl">Date &amp; Time:</span>${b.date||'N/A'} — ${b.time||'N/A'}</div>
    <div class="booking-row"><span class="booking-lbl">🔒 Computers:</span>${pcs}</div>
    <div class="booking-row"><span class="booking-lbl">Purpose:</span>${b.purpose||'N/A'}</div>
    ${mlBlock}${appInfo}${rejInfo}
    <div class="action-row">${cancelBtn}${adminAct}${noshowBtn}${qrBtn}</div>
  </div>`;
}

async function approveBooking(id){try{const r=await fetch('/api/approve-booking',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bookingId:id})});const d=await r.json();if(d.success){toast('✅ Approved','success');loadAdminBookings(adminFilter)}else toast('❌ '+d.message,'error')}catch(e){toast('❌ Error','error')}}
async function rejectBooking(id){const reason=prompt('Rejection reason:');if(!reason)return;try{const r=await fetch('/api/reject-booking',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bookingId:id,reason})});const d=await r.json();if(d.success){toast('❌ Rejected','info');loadAdminBookings(adminFilter)}else toast('❌ '+d.message,'error')}catch(e){}}
async function cancelBooking(id){if(!confirm('Cancel booking? Computers will be released.'))return;try{const r=await fetch('/api/cancel-booking',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bookingId:id})});const d=await r.json();if(d.success){toast('🚫 Cancelled','info');loadMyBookings();if(selTime)renderSeatMap()}else toast('❌ '+d.message,'error')}catch(e){}}
async function markNoShow(id){if(!confirm('Mark as no-show?'))return;try{const r=await fetch('/api/mark-noshow',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bookingId:id})});const d=await r.json();if(d.success){toast('❌ No-show marked','warning');loadAdminBookings(adminFilter)}else toast('❌ '+d.message,'error')}catch(e){}}

/* ── Calendar ─────────────────────────────────────────────────────────────── */
const DAYS=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
const DS=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
function getWeekStart(off){const d=new Date();const day=d.getDay()||7;d.setDate(d.getDate()-day+1+off*7);d.setHours(0,0,0,0);return d}
function fmt(d){return d.toISOString().split('T')[0]}
function calShift(days,who){calOff[who]+=days/7;renderCal(who)}
async function renderCal(who){
  const labEl=document.getElementById(who==='admin'?'acLab':'ucLab');
  const gridEl=document.getElementById(who==='admin'?'aCalGrid':'uCalGrid');
  const lblEl=document.getElementById(who==='admin'?'aCalLbl':'uCalLbl');
  if(!labEl)return;const lab=labEl.value;if(!lab)return;
  const ws=getWeekStart(calOff[who]);const we=new Date(ws);we.setDate(we.getDate()+6);
  lblEl.textContent=`${ws.toLocaleDateString('en-GB',{day:'2-digit',month:'short'})} – ${we.toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'})}`;
  const dates=Array.from({length:7},(_,i)=>{const d=new Date(ws);d.setDate(d.getDate()+i);return d});
  let bMap={};
  try{const r=await fetch('/api/calendar-bookings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lab,from:fmt(ws),to:fmt(we)})});bMap=(await r.json()).bookings||{};}catch(e){}
  let html=`<table class="cal"><thead><tr><th>Time Slot</th>`;
  dates.forEach((d,i)=>{const today=fmt(d)===fmt(new Date());html+=`<th style="${today?'background:#5568d3;':''}">${DS[i]}<br><small>${d.toLocaleDateString('en-GB',{day:'2-digit',month:'short'})}</small></th>`});
  html+=`</tr></thead><tbody>`;
  SLOTS.forEach(slot=>{
    html+=`<tr><td class="slot-label">${slot}</td>`;
    dates.forEach((d,i)=>{
      const dayN=DAYS[i];const ds=fmt(d);const tt=TT[lab]&&TT[lab][dayN]&&TT[lab][dayN][slot];
      const bk=(bMap[ds]&&bMap[ds][slot])||[];const past=d<new Date(new Date().setHours(0,0,0,0));
      html+=`<td><div style="min-height:44px">`;
      if(tt){html+=`<div class="cal-block cal-tt" title="${tt.name}">📚 ${tt.course}<br><small>${tt.name.substring(0,16)}…</small></div>`}
      else if(bk.length){bk.forEach(b=>{const cls=b.status==='approved'?'cal-approved':'cal-pending';const icon=b.status==='approved'?'✅':'⏳';html+=`<div class="cal-block ${cls}">${icon} ${(b.userName||'User').split(' ')[0]}<br><small>🔒PC:${(b.computers||[]).slice(0,3).join(',')}</small></div>`})}
      else if(!past){html+=`<div class="cal-block cal-free">✅ Free</div>`}
      html+=`</div></td>`;
    });html+=`</tr>`;
  });
  html+=`</tbody></table>`;gridEl.innerHTML=html;
}

/* ── Utilities ─────────────────────────────────────────────────────────────── */
function dayName(ds){return ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'][new Date(ds+'T00:00:00').getDay()]}
document.getElementById('classModal').addEventListener('click',e=>{if(e.target.id==='classModal')closeModal('classModal')});
document.getElementById('qrModal').addEventListener('click',e=>{if(e.target.id==='qrModal')closeModal('qrModal')});
document.getElementById('qrVerifyModal').addEventListener('click',e=>{if(e.target.id==='qrVerifyModal')closeModal('qrVerifyModal')});
document.getElementById('forgotModal').addEventListener('click',e=>{if(e.target.id==='forgotModal')closeModal('forgotModal')});

/* ── Chatbot ─────────────────────────────────────────────────────────────── */
let chatHistory=[];
function appendChat(role,text){
  const c=document.getElementById('chatMessages');
  const isUser=role==='user';
  const div=document.createElement('div');
  div.style.cssText=`margin:8px 0;display:flex;justify-content:${isUser?'flex-end':'flex-start'}`;
  div.innerHTML=`<div style="max-width:80%;padding:10px 14px;border-radius:${isUser?'14px 14px 4px 14px':'14px 14px 14px 4px'};
    background:${isUser?'#667eea':'#f0f0f0'};color:${isUser?'white':'#333'};font-size:.9em;line-height:1.5">${text}</div>`;
  c.appendChild(div); c.scrollTop=c.scrollHeight;
}
async function sendChat(){
  const inp=document.getElementById('chatInput');const q=inp.value.trim();
  if(!q)return; inp.value=''; appendChat('user',q);
  appendChat('bot','⏳ Thinking…');
  try{
    const r=await fetch('/api/llm-chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});
    const d=await r.json();
    const msgs=document.getElementById('chatMessages');msgs.lastChild.remove();
    appendChat('bot',d.answer||d.error||'No response');
  }catch(e){document.getElementById('chatMessages').lastChild.remove();appendChat('bot','❌ Error: could not reach chatbot');}
}
async function checkLLMStatus(){
  try{
    const r=await fetch('/api/llm-status');const d=await r.json();
    const badge=document.getElementById('llmStatusBadge');
    const offlineMsg=document.getElementById('llmOfflineMsg');
    const onlineMsg=document.getElementById('llmOnlineMsg');
    if(d.available){
      if(badge){badge.textContent='✅ Model Loaded';badge.style.background='#d4edda';badge.style.color='#155724';}
      if(offlineMsg)offlineMsg.style.display='none';
      if(onlineMsg)onlineMsg.style.display='block';
    }else{
      if(badge){badge.textContent='⚠️ Model Not Loaded';badge.style.background='#fff3cd';badge.style.color='#856404';}
      if(offlineMsg)offlineMsg.style.display='block';
      if(onlineMsg)onlineMsg.style.display='none';
    }
  }catch(e){}
}

/* ── Show / Hide Password ────────────────────────────────────────────────── */
function togglePw(inputId, btn){
  const inp = document.getElementById(inputId);
  if(inp.type === 'password'){
    inp.type = 'text';
    btn.textContent = '🙈';
    btn.title = 'Hide password';
  } else {
    inp.type = 'password';
    btn.textContent = '👁️';
    btn.title = 'Show password';
  }
}

// Restore session on load

/* ── Forgot Password ─────────────────────────────────────────────────────── */
function showForgotPassword(){
  document.getElementById('fpUsername').value='';
  document.getElementById('fpErr').style.display='none';
  document.getElementById('fpStep1').style.display='block';
  document.getElementById('fpStep2').style.display='none';
  document.getElementById('forgotModal').classList.add('show');
}
async function submitForgotPassword(){
  const username=document.getElementById('fpUsername').value.trim();
  const err=document.getElementById('fpErr');
  const btn=document.getElementById('fpSendBtn');
  err.style.display='none';
  if(!username){err.textContent='❌ Please enter your username';err.style.display='block';return}
  btn.disabled=true;btn.textContent='Sending…';
  try{
    const r=await fetch('/api/forgot-password',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username})});
    const d=await r.json();
    if(d.success){
      document.getElementById('fpStep1').style.display='none';
      document.getElementById('fpStep2').style.display='block';
      if(d.hint){
        document.getElementById('fpStep2Msg').textContent = 'Reset link sent to ' + d.hint.replace('Reset link sent to ','') + '. Click it to set your new password. Expires in 30 min.';
      }
    } else {
      err.textContent='❌ '+(d.message||'Error');err.style.display='block';
    }
  }catch(e){err.textContent='❌ Network error';err.style.display='block'}
  finally{btn.disabled=false;btn.textContent='📧 Send Reset Link'}
}

/* ── Set New Password (after clicking reset link) ─────────────────────────── */
let _resetToken = null;
async function submitNewPassword(){
  const p=document.getElementById('rpNewPass').value;
  const pc=document.getElementById('rpNewPassC').value;
  const err=document.getElementById('rpErr');
  const btn=document.getElementById('rpBtn');
  err.style.display='none';
  if(p.length<6){err.textContent='❌ Password must be at least 6 characters';err.style.display='block';return}
  if(p!==pc){err.textContent='❌ Passwords do not match';err.style.display='block';return}
  btn.disabled=true;btn.textContent='Saving…';
  try{
    const r=await fetch('/api/reset-password-token',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token:_resetToken,new_password:p})});
    const d=await r.json();
    if(d.success){
      const uname = d.username ? ` Login with username: <b>${d.username}</b>` : '';
      document.getElementById('rpSuccess').innerHTML = `✅ Password changed!${uname}. You can now <a href="/" style="color:#155724;font-weight:700">login</a>.`;
      document.getElementById('rpSuccess').style.display='block';
      document.getElementById('rpBtn').style.display='none';
      document.getElementById('rpNewPass').disabled=true;
      document.getElementById('rpNewPassC').disabled=true;
    } else {
      err.textContent='❌ '+(d.message||'Error');err.style.display='block';
    }
  }catch(e){err.textContent='❌ Network error';err.style.display='block'}
  finally{btn.disabled=false;btn.textContent='Set New Password'}
}

// Restore session on load + handle Google OAuth redirect
(async()=>{
  const params=new URLSearchParams(window.location.search);

  if(params.get('oauth')==='register'){
    // Google auth succeeded, new user → pre-fill register form
    history.replaceState({},'','/');
    const r=await fetch('/api/google-prefill');
    const d=await r.json();
    if(d.email){
      document.getElementById('re').value=d.email;
      document.getElementById('rn').value=d.name||'';
      // Password fields always visible — user must set a password to enable username/password login
    }
    showReg();
    toast('✅ Google sign-in done! Set a username, password and department to complete registration.','success');
    return;
  }

  if(params.get('oauth')==='done'){
    // Already registered Google user → restore session and enter
    history.replaceState({},'','/');
    try{const r=await fetch('/api/session');const d=await r.json();if(d.logged_in){cu=d.user;initMain();checkLLMStatus()}}catch(e){}
    return;
  }

  if(params.get('oauth')==='error'){
    history.replaceState({},'','/');
    toast('❌ Google login error: '+params.get('msg'),'error');
    return;
  }

  // Check if arriving via password reset link
  if(params.get('token')){
    _resetToken = params.get('token');
    history.replaceState({},'','/');
    document.getElementById('authSection').style.display='none';
    document.getElementById('resetPasswordPage').style.display='block';
    return;
  }

  // Normal page load — restore existing session
  try{const r=await fetch('/api/session');const d=await r.json();if(d.logged_in){cu=d.user;initMain();checkLLMStatus()}}catch(e){}
})();
</script>
</body></html>'''

# ═══════════════════════════════════════════════════════════════════════════════
#  API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    lab_options = ''.join(f'<option value="{lid}">{li["name"]}</option>' for lid,li in LABS.items())
    page = HTML.replace('__LAB_OPTIONS__', lab_options)
    page = page.replace('__TIMETABLE__', json.dumps(TIMETABLE))
    page = page.replace('__LABS__', json.dumps(LABS))
    page = page.replace('__SLOTS__', json.dumps(TIME_SLOTS))
    return page

@app.route('/api/session')
def check_session():
    if 'user_id' not in session: return jsonify({'logged_in':False})
    user=get_user_data(session['user_id'])
    if not user: session.clear(); return jsonify({'logged_in':False})
    return jsonify({'logged_in':True,'user':{'username':user.get('username',session['user_id']),'name':user.get('name','User'),'role':user.get('role','student'),'email':user.get('email','')}})

@app.route('/api/login', methods=['POST'])
def login():
    try:
        d=request.json; u=d.get('username','').strip(); p=d.get('password','')
        if not u or not p:
            return jsonify({'success':False,'message':'Username and password are required'})
        # 1. Fallback hardcoded users (admin/faculty1)
        fallback=verify_fallback_user(u,p)
        if fallback:
            session['user_id']=u; session['user_role']=fallback['role']
            return jsonify({'success':True,'user':{'username':u,'name':fallback['name'],'role':fallback['role'],'email':fallback['email']}})
        # 2. Firebase users
        if db:
            user=None; uid=None
            ref=db.collection('users').document(u).get()
            if ref.exists: user=ref.to_dict(); uid=u
            else:
                for doc in db.collection('users').where('username','==',u).limit(1).stream():
                    user=doc.to_dict(); uid=doc.id
            if user and verify_password(p,user.get('password','')):
                session['user_id']=uid; session['user_role']=user['role']
                return jsonify({'success':True,'user':{'username':user.get('username',uid),'name':user.get('name','User'),'role':user.get('role','student'),'email':user.get('email','')}})
        # 3. In-memory / file-backed users — always reload fresh from disk
        fresh_users = _load_users()
        _mem_users.update(fresh_users)   # sync in-memory with disk
        for uid,udata in _mem_users.items():
            if udata.get('username','').lower()==u.lower() and verify_password(p, udata.get('password','')):
                session['user_id']=uid; session['user_role']=udata.get('role','student')
                return jsonify({'success':True,'user':{'username':udata.get('username',uid),'name':udata.get('name','User'),'role':udata.get('role','student'),'email':udata.get('email','')}})
        return jsonify({'success':False,'message':'Invalid username or password'})
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({'success':False,'message':str(e)})

@app.route('/api/logout', methods=['POST'])
def logout_api():
    uid=session.get('user_id')
    if uid:
        with _sse_lock:
            _sse_clients.pop(uid,None)
    session.clear(); return jsonify({'success':True})

@app.route('/api/debug-users')
def debug_users():
    """Debug: lists all usernames stored (no passwords). Visit in browser."""
    fresh = _load_users()
    users_list = [{'username': v.get('username'), 'email': v.get('email'), 'role': v.get('role'), 'name': v.get('name')} for v in fresh.values()]
    return jsonify({'users_file': _USERS_FILE, 'file_exists': os.path.exists(_USERS_FILE), 'count': len(users_list), 'users': users_list})

@app.route('/api/register', methods=['POST'])
def register():
    try:
        d=request.json; u=d.get('username','').strip(); e=d.get('email','').strip()
        if not u: return jsonify({'success':False,'message':'Username is required'})
        if not e: return jsonify({'success':False,'message':'Email is required'})
        pw = '' if d.get('google_auth') else hash_password(d.get('password',''))
        new_user = {'username':u,'password':pw,'email':e,'name':d.get('name','').strip(),'role':'student','department':d.get('department',''),'google_auth':bool(d.get('google_auth')),'createdAt':datetime.now().isoformat()}
        if db:
            for _ in db.collection('users').where('username','==',u).limit(1).stream():
                return jsonify({'success':False,'message':'Username already taken'})
            db.collection('users').document().set(new_user)
        else:
            for uid,udata in _mem_users.items():
                if udata.get('username','').lower()==u.lower(): return jsonify({'success':False,'message':'Username already taken'})
            if u in FALLBACK_USERS: return jsonify({'success':False,'message':'Username already taken'})
            new_uid = _new_id()
            _mem_users[new_uid] = new_user
            _save_users()
        return jsonify({'success':True})
    except Exception as e: return jsonify({'success':False,'message':str(e)})

@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    """Step 1: accept username, find their email, send reset link."""
    try:
        data = request.json or {}
        username = data.get('username','').strip()
        if not username:
            return jsonify({'success':False,'message':'Username is required'})

        # Find user record by username
        found_uid = None; found_user = None
        if db:
            for doc in db.collection('users').where('username','==',username).limit(1).stream():
                found_user = doc.to_dict(); found_uid = doc.id; break
            # fallback: case-insensitive search
            if not found_user:
                for doc in db.collection('users').stream():
                    ud = doc.to_dict()
                    if ud.get('username','').lower() == username.lower():
                        found_user = ud; found_uid = doc.id; break
        else:
            # Always reload fresh from disk first
            fresh = _load_users()
            _mem_users.update(fresh)
            for uid, udata in _mem_users.items():
                if udata.get('username','').lower() == username.lower():
                    found_user = udata; found_uid = uid; break

        if not found_user:
            # Give a helpful error — username doesn't exist
            return jsonify({'success':False,
                'message':f'No account found with username "{username}". Please register first.'})

        user_email = found_user.get('email','')
        user_name  = found_user.get('name', username)
        if not user_email:
            return jsonify({'success':False,'message':'No email address on this account. Please contact admin.'})

        # Generate token — store both email AND username for reliable lookup
        token = secrets.token_urlsafe(32)
        _reset_tokens[token] = {
            'email': user_email.lower(),
            'username': username,
            'uid': found_uid,
            'expires': datetime.now() + timedelta(minutes=30)
        }
        email_password_reset(user_email, user_name, token)
        print(f"🔑 Reset link for '{username}' ({user_email}): http://localhost:5000/reset-password?token={token}")
        return jsonify({'success': True, 'hint': f'Reset link sent to {user_email[:3]}***'})
    except Exception as e:
        print(f"Forgot-password error: {e}")
        return jsonify({'success':False,'message':str(e)})

@app.route('/reset-password')
def reset_password_page():
    """Redirect to main page — the JS handles the ?token= param.
    Clear any existing session so the user must log in fresh after reset."""
    session.clear()
    return redirect("/?token=" + request.args.get("token",""))

@app.route('/api/reset-password-token', methods=['POST'])
def reset_password_token():
    """Step 2: validate token and set new password."""
    try:
        d = request.json or {}
        token = d.get('token','').strip()
        new_pw = d.get('new_password','')
        if not token: return jsonify({'success':False,'message':'Invalid or expired reset link'})
        if len(new_pw) < 6: return jsonify({'success':False,'message':'Password must be at least 6 characters'})
        info = _reset_tokens.get(token)
        if not info: return jsonify({'success':False,'message':'Reset link is invalid or has expired. Please request a new one.'})
        if datetime.now() > info['expires']:
            del _reset_tokens[token]
            return jsonify({'success':False,'message':'Reset link has expired (30 min limit). Please request a new one.'})
        hashed = hash_password(new_pw)
        updated = False
        # Always reload from disk first
        fresh = _load_users()
        _mem_users.update(fresh)
        if db:
            uid_direct = info.get('uid')
            if uid_direct:
                try:
                    db.collection('users').document(uid_direct).update({'password': hashed})
                    updated = True
                except: pass
            if not updated:
                for doc in db.collection('users').stream():
                    ud = doc.to_dict()
                    if ud.get('username','').lower() == info.get('username','').lower():
                        db.collection('users').document(doc.id).update({'password': hashed})
                        updated = True; break
        else:
            # Try by stored uid first (fastest)
            uid_direct = info.get('uid')
            if uid_direct and uid_direct in _mem_users:
                _mem_users[uid_direct]['password'] = hashed
                updated = True
            # Fallback: search by username
            if not updated:
                for uid, udata in _mem_users.items():
                    if udata.get('username','').lower() == info.get('username','').lower():
                        _mem_users[uid]['password'] = hashed
                        updated = True; break
            # Last resort: search by email
            if not updated:
                for uid, udata in _mem_users.items():
                    if udata.get('email','').lower() == info.get('email','').lower():
                        _mem_users[uid]['password'] = hashed
                        updated = True; break
        if not updated:
            return jsonify({'success':False,'message':'Account not found. Please register first.'})
        _save_users()
        _mem_users.update(_load_users())
        del _reset_tokens[token]
        print(f"✅ Password reset for username: {info.get('username')}")
        return jsonify({'success': True, 'username': info.get('username','')})
    except Exception as e:
        print(f"Reset-password-token error: {e}")
        return jsonify({'success':False,'message':str(e)})

@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    try:
        d = request.json
        u = d.get('username','').strip()
        new_pw = d.get('new_password','')
        if not u: return jsonify({'success':False,'message':'Username is required'})
        if len(new_pw) < 6: return jsonify({'success':False,'message':'Password must be at least 6 characters'})
        if u in FALLBACK_USERS: return jsonify({'success':False,'message':'Cannot reset password for built-in accounts'})
        hashed = hash_password(new_pw)
        if db:
            found = False
            for doc in db.collection('users').where('username','==',u).limit(1).stream():
                db.collection('users').document(doc.id).update({'password': hashed})
                found = True
            if not found: return jsonify({'success':False,'message':'Username not found'})
        else:
            found = False
            for uid, udata in _mem_users.items():
                if udata.get('username') == u:
                    _mem_users[uid]['password'] = hashed
                    found = True; break
            if not found: return jsonify({'success':False,'message':'Username not found'})
        _save_users()
        return jsonify({'success':True})
    except Exception as e: return jsonify({'success':False,'message':str(e)})

# ── Google OAuth Routes ───────────────────────────────────────────────────────
@app.route('/auth/google')
def google_login():
    try:
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_config(
            {"web":{"client_id":GOOGLE_CLIENT_ID,"client_secret":GOOGLE_CLIENT_SECRET,
                    "auth_uri":"https://accounts.google.com/o/oauth2/auth",
                    "token_uri":"https://oauth2.googleapis.com/token",
                    "redirect_uris":[GOOGLE_REDIRECT_URI]}},
            scopes=["openid","https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile"],
            redirect_uri=GOOGLE_REDIRECT_URI)
        auth_url, state = flow.authorization_url()
        session['oauth_state'] = state
        return jsonify({'auth_url': auth_url})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/callback')
def google_callback():
    try:
        from google_auth_oauthlib.flow import Flow
        from google.oauth2 import id_token
        import google.auth.transport.requests
        flow = Flow.from_client_config(
            {"web":{"client_id":GOOGLE_CLIENT_ID,"client_secret":GOOGLE_CLIENT_SECRET,
                    "auth_uri":"https://accounts.google.com/o/oauth2/auth",
                    "token_uri":"https://oauth2.googleapis.com/token",
                    "redirect_uris":[GOOGLE_REDIRECT_URI]}},
            scopes=["openid","https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile"],
            redirect_uri=GOOGLE_REDIRECT_URI)
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        req = google.auth.transport.requests.Request()
        id_info = id_token.verify_oauth2_token(creds.id_token, req, GOOGLE_CLIENT_ID, clock_skew_in_seconds=10)
        google_email = id_info.get('email','')
        google_name  = id_info.get('name','')
        # Check if user already exists
        if db:
            for doc in db.collection('users').where('email','==',google_email).limit(1).stream():
                u = doc.to_dict(); uid = doc.id
                if u.get('username'):
                    session['user_id']   = uid
                    session['user_role'] = u.get('role','student')
                    return redirect('/?oauth=done')
        else:
            for uid,udata in _mem_users.items():
                if udata.get('email')==google_email and udata.get('username'):
                    session['user_id']   = uid
                    session['user_role'] = udata.get('role','student')
                    return redirect('/?oauth=done')
        # New user → send to register page with pre-filled data
        session['google_prefill'] = {'email': google_email, 'name': google_name}
        return redirect('/?oauth=register')
    except Exception as e:
        return redirect(f'/?oauth=error&msg={str(e)}')

@app.route('/api/google-prefill')
def google_prefill():
    data = session.pop('google_prefill', {})   # pop so it's used once
    return jsonify(data)


# ── SSE Notification Stream ──────────────────────────────────────────────────
@app.route('/api/notify-stream')
def notify_stream():
    if 'user_id' not in session:
        return Response('data: {"type":"error"}\n\n', mimetype='text/event-stream')
    uid = session['user_id']
    q = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_clients.setdefault(uid, []).append(q)

    def generate():
        try:
            # Send heartbeat immediately
            yield f'data: {json.dumps({"type":"heartbeat"})}\n\n'
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f'data: {json.dumps(msg)}\n\n'
                except queue.Empty:
                    yield f'data: {json.dumps({"type":"heartbeat"})}\n\n'
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                lst = _sse_clients.get(uid, [])
                if q in lst: lst.remove(q)

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

# ── QR Code endpoints ────────────────────────────────────────────────────────
@app.route('/api/qr/<booking_id>')
def get_qr(booking_id):
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'})
    b = get_booking(booking_id)
    if not b: return jsonify({'error':'Not found'})
    # Only owner or admin can view QR
    if b.get('userId') != session['user_id'] and session.get('user_role') != 'admin':
        return jsonify({'error':'Unauthorized'})
    qr_text  = booking_to_qr_text(b)
    qr_image = make_qr_image_b64(b)   # PNG (or SVG fallback)
    return jsonify({
        'qr_image': qr_image,          # base64 data URI — use in <img src="...">
        'qr_text':  qr_text,           # raw text encoded in QR
        'lab':    LABS.get(b.get('lab',''),{}).get('name', b.get('lab','')),
        'date':   b.get('date',''),
        'time':   b.get('time',''),
        'status': b.get('status',''),
        'userName':  b.get('userName',''),
        'computers': b.get('computers',[]),
        'purpose':   b.get('purpose',''),
    })

@app.route('/api/qr-verify', methods=['POST'])
def qr_verify():
    if session.get('user_role') != 'admin': return jsonify({'valid':False,'message':'Admin only'})
    try:
        qr_text = request.json.get('qr_text','').strip()
        parts = qr_text.split('|')
        if len(parts) < 6 or parts[0] != 'SASTRA':
            return jsonify({'valid':False,'message':'Invalid QR format — not a SASTRA booking QR'})
        bid_prefix = parts[1]
        # Find booking matching prefix
        all_b = get_all_bookings()
        match = next((b for b in all_b if str(b.get('id','')).startswith(bid_prefix)), None)
        if not match:
            return jsonify({'valid':False,'message':f'No booking found matching ID prefix: {bid_prefix}'})
        if match.get('status') not in ('approved','pending'):
            return jsonify({'valid':False,'message':f'Booking is {match.get("status","unknown")} — not valid for lab entry'})
        return jsonify({'valid':True,'booking':match})
    except Exception as e: return jsonify({'valid':False,'message':str(e)})

# ── Other routes (same as v3) ────────────────────────────────────────────────
@app.route('/api/ml-preview', methods=['POST'])
def ml_preview():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'})
    d=request.json; role=session.get('user_role','student')
    ns,total=ml_engine.get_user_stats(db,session['user_id'])
    return jsonify(ml_engine.predict(d.get('lab',''),d.get('date',''),d.get('time',''),int(d.get('num_computers',1)),d.get('purpose',''),role,ns,total))

@app.route('/api/demand-forecast', methods=['POST'])
def demand_forecast():
    d=request.json; return jsonify({'forecast':ml_engine.demand_forecast(d.get('lab',''),d.get('date',''))})

@app.route('/api/ml-stats')
def ml_stats():
    return jsonify({'trained':ml_engine.trained,**ml_engine.training_stats})

@app.route('/api/retrain-ml', methods=['POST'])
def retrain_ml():
    if session.get('user_role')!='admin': return jsonify({'success':False})
    threading.Thread(target=lambda:ml_engine.train(db_ref=db),daemon=True).start()
    return jsonify({'success':True})

@app.route('/api/available-computers', methods=['POST'])
def available_computers():
    if 'user_id' not in session: return jsonify({'success':False})
    d=request.json; lab=d.get('lab'); date=d.get('date'); tslot=d.get('time')
    booked_pcs=set(); booked_details=[]
    for b in get_bookings_for_slot(lab,date,tslot):
        pcs=b.get('computers',[]); booked_pcs.update(pcs)
        booked_details.append({'bookingId':b.get('id',''),'pcs':pcs,'userName':b.get('userName','User'),'isMe':b.get('userId')==session['user_id'],'status':b.get('status','pending')})
    return jsonify({'success':True,'available':sorted(list(set(range(1,34))-booked_pcs)),'booked':sorted(list(booked_pcs)),'booked_details':booked_details})

@app.route('/api/book', methods=['POST'])
def create_booking():
    if 'user_id' not in session: return jsonify({'success':False,'message':'Not authenticated'})
    if not check_rate_limit(session['user_id']): return jsonify({'success':False,'message':'Too many bookings this hour (max 10)'})
    try:
        d=request.json; lab=d['lab']; date=d['date']; tslot=d['time']
        req_pcs=set(d['computers']); purpose=d.get('purpose',''); role=session.get('user_role','student')
        booked_pcs=set()
        for b in get_bookings_for_slot(lab,date,tslot): booked_pcs.update(b.get('computers',[]))
        conflicts=req_pcs.intersection(booked_pcs)
        if conflicts: return jsonify({'success':False,'message':f"🔒 Computers {sorted(conflicts)} are already locked by another booking."})
        user_data=get_user_data(session['user_id']) or {'name':'User','email':'','role':role}
        ns,total=ml_engine.get_user_stats(db,session['user_id'])
        ml=ml_engine.predict(lab,date,tslot,len(req_pcs),purpose,role,ns,total)
        lab_name=LABS.get(lab,{}).get('name',lab)
        booking_data={'userId':session['user_id'],'userName':user_data.get('name','User'),'userEmail':user_data.get('email',''),'userRole':role,'lab':lab,'date':date,'time':tslot,'computers':list(req_pcs),'purpose':purpose,'status':'pending','ml_recommendation':ml['recommendation'],'ml_confidence':ml['xgb_confidence'],'ml_reason':ml['ml_reason'],'ml_demand_level':ml['demand_level'],'is_anomaly':ml['is_anomaly'],'anomaly_score':ml['anomaly_score'],'models_used':ml['models_used'],'createdAt':datetime.now().isoformat(),'requestedAt':datetime.now().isoformat(),'reminderSent':False,'noShow':False,'cancelled':False}
        bid=save_booking(booking_data)
        email_received(user_data.get('email',''),user_data.get('name','User'),bid,lab_name,date,tslot)
        audit_log('BOOKING_CREATED',session['user_id'],{'bookingId':bid,'lab':lab,'date':date,'time':tslot})
        return jsonify({'success':True,'bookingId':bid,'ml_reason':ml['ml_reason']})
    except Exception as e: return jsonify({'success':False,'message':str(e)})

@app.route('/api/cancel-booking', methods=['POST'])
def cancel_booking():
    if 'user_id' not in session: return jsonify({'success':False,'message':'Unauthorized'})
    try:
        bid=request.json.get('bookingId'); b=get_booking(bid)
        if not b: return jsonify({'success':False,'message':'Booking not found'})
        if b.get('userId')!=session['user_id'] and session.get('user_role')!='admin': return jsonify({'success':False,'message':'You can only cancel your own bookings'})
        if b.get('status') not in ('pending','approved'): return jsonify({'success':False,'message':'Only pending/approved bookings can be cancelled'})
        update_booking(bid,{'status':'cancelled','cancelledAt':datetime.now().isoformat(),'cancelledBy':session['user_id']})
        lab_name=LABS.get(b.get('lab',''),{}).get('name',b.get('lab',''))
        email_cancelled(b.get('userEmail',''),b.get('userName',''),lab_name,b.get('date',''),b.get('time',''))
        audit_log('BOOKING_CANCELLED',session['user_id'],{'bookingId':bid})
        _notify_waitlist(b.get('lab',''),b.get('date',''),b.get('time',''))
        return jsonify({'success':True})
    except Exception as e: return jsonify({'success':False,'message':str(e)})

@app.route('/api/waitlist', methods=['POST'])
def add_waitlist():
    if 'user_id' not in session: return jsonify({'success':False,'message':'Unauthorized'})
    try:
        d=request.json; lab=d.get('lab'); date=d.get('date'); tslot=d.get('time')
        user=get_user_data(session['user_id']) or {'name':'User','email':''}
        if db:
            for _ in db.collection('waitlist').where('userId','==',session['user_id']).where('lab','==',lab).where('date','==',date).where('time','==',tslot).limit(1).stream():
                return jsonify({'success':False,'message':'Already on waitlist'})
            db.collection('waitlist').document().set({'userId':session['user_id'],'userName':user.get('name','User'),'userEmail':user.get('email',''),'lab':lab,'date':date,'time':tslot,'notified':False,'createdAt':datetime.now().isoformat()})
        else:
            for w in _mem_waitlist.values():
                if w['userId']==session['user_id'] and w['lab']==lab and w['date']==date and w['time']==tslot:
                    return jsonify({'success':False,'message':'Already on waitlist'})
            wid=_new_id(); _mem_waitlist[wid]={'userId':session['user_id'],'userName':user.get('name','User'),'userEmail':user.get('email',''),'lab':lab,'date':date,'time':tslot,'notified':False,'createdAt':datetime.now().isoformat()}
        return jsonify({'success':True})
    except Exception as e: return jsonify({'success':False,'message':str(e)})

@app.route('/api/mark-noshow', methods=['POST'])
def mark_noshow():
    if session.get('user_role')!='admin': return jsonify({'success':False,'message':'Admin only'})
    try:
        bid=request.json.get('bookingId'); b=get_booking(bid)
        if not b: return jsonify({'success':False,'message':'Not found'})
        update_booking(bid,{'noShow':True,'noShowAt':datetime.now().isoformat()})
        audit_log('NO_SHOW_MARKED',session['user_id'],{'bookingId':bid})
        return jsonify({'success':True})
    except Exception as e: return jsonify({'success':False,'message':str(e)})

@app.route('/api/my-bookings')
def my_bookings():
    if 'user_id' not in session: return jsonify({'bookings':[]})
    return jsonify({'bookings':get_user_bookings(session['user_id'])})

@app.route('/api/all-bookings')
def all_bookings():
    if session.get('user_role')!='admin': return jsonify({'bookings':[],'counts':{'pending':0,'approved':0,'rejected':0,'total':0}})
    sf=request.args.get('status','all'); results=get_all_bookings(sf)
    counts={'pending':sum(1 for b in results if b.get('status')=='pending'),'approved':sum(1 for b in results if b.get('status')=='approved'),'rejected':sum(1 for b in results if b.get('status')=='rejected'),'total':len(results)}
    return jsonify({'bookings':results,'counts':counts})

@app.route('/api/approve-booking', methods=['POST'])
def approve_booking():
    if session.get('user_role')!='admin': return jsonify({'success':False,'message':'Unauthorized'})
    try:
        bid=request.json.get('bookingId'); admin=get_user_data(session['user_id']) or {'name':'Admin'}
        bdata=get_booking(bid)
        if not bdata: return jsonify({'success':False,'message':'Not found'})
        update_booking(bid,{'status':'approved','approvedBy':admin.get('name','Admin'),'approvedAt':datetime.now().isoformat()})
        full_b=get_booking(bid)
        lab_name=LABS.get(bdata.get('lab',''),{}).get('name',bdata.get('lab',''))
        email_approved(bdata.get('userEmail',''),bdata.get('userName',''),bid,lab_name,bdata.get('date',''),bdata.get('time',''),bdata.get('computers',[]),admin.get('name','Admin'),full_b)
        # Push to user
        sse_push(bdata.get('userId',''),'approved',{
            'title':'✅ Booking Approved!',
            'message':f"Your booking for {lab_name} on {bdata.get('date','')} was approved by {admin.get('name','Admin')}.",
            'bookingId':bid})
        audit_log('APPROVED',session['user_id'],{'bookingId':bid})
        return jsonify({'success':True})
    except Exception as e: return jsonify({'success':False,'message':str(e)})

@app.route('/api/reject-booking', methods=['POST'])
def reject_booking():
    if session.get('user_role')!='admin': return jsonify({'success':False,'message':'Unauthorized'})
    try:
        bid=request.json.get('bookingId'); reason=request.json.get('reason','No reason')
        admin=get_user_data(session['user_id']) or {'name':'Admin'}
        bdata=get_booking(bid)
        if not bdata: return jsonify({'success':False,'message':'Not found'})
        update_booking(bid,{'status':'rejected','rejectedBy':admin.get('name','Admin'),'rejectedAt':datetime.now().isoformat(),'rejectionReason':reason})
        lab_name=LABS.get(bdata.get('lab',''),{}).get('name',bdata.get('lab',''))
        email_rejected(bdata.get('userEmail',''),bdata.get('userName',''),lab_name,bdata.get('date',''),bdata.get('time',''),reason,admin.get('name','Admin'))
        # Push to user
        sse_push(bdata.get('userId',''),'rejected',{
            'title':'❌ Booking Rejected',
            'message':f"Your booking for {lab_name} on {bdata.get('date','')} was rejected. Reason: {reason}",
            'bookingId':bid})
        audit_log('REJECTED',session['user_id'],{'bookingId':bid,'reason':reason})
        return jsonify({'success':True})
    except Exception as e: return jsonify({'success':False,'message':str(e)})

@app.route('/api/calendar-bookings', methods=['POST'])
def calendar_bookings():
    d=request.json; lab=d.get('lab'); frm=d.get('from'); to=d.get('to')
    all_b=[]
    if db:
        try:
            for doc in db.collection('bookings').where('lab','==',lab).stream(): b=doc.to_dict(); b['id']=doc.id; all_b.append(b)
        except: pass
    else:
        for bid,b in _mem_bookings.items():
            if b['lab']==lab: all_b.append({**b,'id':bid})
    result={}
    for b in all_b:
        date=b.get('date',''); slot=b.get('time','')
        if not(frm<=date<=to): continue
        if b.get('status') not in ('pending','approved'): continue
        result.setdefault(date,{}).setdefault(slot,[]).append({'userName':b.get('userName','User'),'status':b.get('status','pending'),'computers':b.get('computers',[]),'purpose':b.get('purpose','')})
    return jsonify({'bookings':result})

@app.route('/api/llm-chat', methods=['POST'])
def llm_chat():
    if 'user_id' not in session: return jsonify({'error':'Unauthorized'})
    question = (request.json or {}).get('question','').strip()
    if not question: return jsonify({'error':'No question provided'})
    answer = llm_generate(question)
    return jsonify({'answer': answer, 'llm_available': LLM_AVAILABLE})

@app.route('/api/llm-status')
def llm_status():
    return jsonify({'available': LLM_AVAILABLE})

# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("\n"+"="*70)
    print("🎓  SASTRA LAB BOOKING v4 — QR CODES + PUSH NOTIFICATIONS")
    print("="*70)
    print(f"\n🔥  Firebase:     {'✅ Connected' if db else '⚠️  Not connected (in-memory mode)'}")
    print(f"📧  Email:        {'✅ Ready' if MAIL_AVAILABLE else '⚠️  Not configured (emails printed to console)'}")
    print(f"🤖  ML:           {'✅ Available' if ML_AVAILABLE else '⚠️  Using rule-based fallback'}")
    print(f"🧠  LLM Chatbot:  {'✅ Loaded' if LLM_AVAILABLE else '⚠️  Not loaded (run train_lab_llm.py to enable)'}")
    print("\n🔑  Logins (always work):")
    print("     admin    / admin123")
    print("     faculty1 / faculty123")
    print("\n📱  QR CODES:")
    print("     → Every approved booking shows a 📱 QR button")
    print("     → QR encodes: BookingID|Lab|Date|Time|PCs|Name")
    print("     → Admin can click 'Verify QR' to validate at lab entrance")
    print("     → QR also embedded in approval email")
    print("\n🔔  PUSH NOTIFICATIONS:")
    print("     → Browser asks permission on login → click 'Enable'")
    print("     → Instant OS-level alert when booking is approved/rejected")
    print("     → Also works as in-app toast notification")
    print("     → Powered by Web Notifications API + Server-Sent Events (SSE)")
    print("     → 30-min session reminders also pushed")
    print("\n🌐  http://localhost:5000")
    print("="*70+"\n")

    ml_engine.train(db_ref=None, force_synthetic=True)
    if db: init_firebase()
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)