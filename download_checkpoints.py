"""
Downloads vocab.pkl and best.pt from Google Drive before app starts.
Called automatically by Procfile before gunicorn.
"""
import os, sys, urllib.request

CHECKPOINTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints")

FILES = {
    "vocab.pkl": "152U_AIJfoAX5kRiQzyvtPH9ufK2kVyfv",
    "best.pt":   "1vZvQpfBTTg_o61mVC3vxUM7REsB-Gimm",
}

def download_file(filename, file_id):
    dest = os.path.join(CHECKPOINTS_DIR, filename)
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        print(f"✅ {filename} already exists ({os.path.getsize(dest)//1024} KB) — skipping")
        return True
    url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    print(f"⬇️  Downloading {filename}...")
    try:
        urllib.request.urlretrieve(url, dest)
        size = os.path.getsize(dest)
        if size < 1024:
            print(f"⚠️  {filename} too small ({size} bytes) — download may have failed")
            os.remove(dest)
            return False
        print(f"✅ {filename} ready ({size//1024} KB)")
        return True
    except Exception as e:
        print(f"❌ Failed: {e}")
        return False

def main():
    os.makedirs(CHECKPOINTS_DIR, exist_ok=True)
    print("\n" + "="*50)
    print("📦 Downloading LLM checkpoints...")
    print("="*50)
    for filename, file_id in FILES.items():
        download_file(filename, file_id)
    print("\n✅ Done\n")
    sys.exit(0)

if __name__ == "__main__":
    main()
