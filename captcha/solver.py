import base64
import requests
import os
from dotenv import load_dotenv

load_dotenv()

CAPSOLVER_KEY = os.getenv("CAPSOLVER_API_KEY", "")

# Built once and reused for every CAPTCHA attempt, instead of rebuilding a
# fresh ddddocr engine (which reloads its OCR model from scratch) on every
# single call. KPPP alone retries up to 6 times per search/download, and
# with the Claude vision step currently failing on every attempt (API
# credit balance exhausted), this fallback was being hit constantly —
# repeatedly recreating the engine is wasteful and was suspected of
# occasionally triggering a native-level hang that froze the whole app.
_ocr_engine = None

def _get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        import ddddocr
        _ocr_engine = ddddocr.DdddOcr(show_ad=False)
    return _ocr_engine

def solve_captcha_local(image_bytes: bytes) -> str:
    """Try to solve CAPTCHA locally using ddddocr (free)."""
    try:
        from PIL import Image, ImageFilter, ImageEnhance
        import io

        # Open image
        img = Image.open(io.BytesIO(image_bytes))
        
        # Crop black border
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        
        # Convert to RGB
        img = img.convert("RGB")
        
        # Enhance contrast
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)
        
        # Enhance sharpness
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(2.0)
        
        # Convert back to bytes
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        processed_bytes = buf.getvalue()
        
        # Solve with ddddocr
        ocr = _get_ocr_engine()
        result = ocr.classification(processed_bytes)
        
        # Clean result - keep only alphanumeric
        import re
        result = re.sub(r'[^A-Za-z0-9]', '', result)
        
        return result.strip().upper()
    except Exception as e:
        print(f"Local CAPTCHA solver failed: {e}")
        return ""

def solve_captcha_api(image_bytes: bytes) -> str:
    """Fallback: solve CAPTCHA using CapSolver API (paid)."""
    if not CAPSOLVER_KEY:
        return ""
    try:
        import time
        b64 = base64.b64encode(image_bytes).decode()
        response = requests.post("https://api.capsolver.com/createTask", json={
            "clientKey": CAPSOLVER_KEY,
            "task": {"type": "ImageToTextTask", "body": b64}
        }, timeout=30)
        task_id = response.json().get("taskId")
        if not task_id:
            return ""
        for _ in range(10):
            time.sleep(3)
            result = requests.post("https://api.capsolver.com/getTaskResult", json={
                "clientKey": CAPSOLVER_KEY,
                "taskId": task_id
            }, timeout=15).json()
            if result.get("status") == "ready":
                return result.get("solution", {}).get("text", "")
        return ""
    except Exception as e:
        print(f"CapSolver API failed: {e}")
        return ""

def solve_captcha(image_bytes: bytes) -> str:
    """
    Hybrid solver:
    1. Try Claude vision first (most accurate)
    2. Fall back to ddddocr
    3. Fall back to CapSolver API
    """
    print("Attempting Claude vision CAPTCHA solve...")
    result = solve_captcha_claude(image_bytes)
    if result and len(result) >= 3:
        return result
    
    print("Attempting local CAPTCHA solve...")
    result = solve_captcha_local(image_bytes)
    if result and len(result) >= 3:
        print(f"Local solver succeeded: {result}")
        return result
    
    print("Local failed, trying CapSolver API...")
    result = solve_captcha_api(image_bytes)
    if result:
        print(f"CapSolver succeeded: {result}")
        return result
    
    print("Both CAPTCHA solvers failed")
    _save_failed_captcha(image_bytes)
    return ""

def _save_failed_captcha(image_bytes: bytes):
    """Dump a CAPTCHA image every solver failed on, for later inspection."""
    try:
        import os
        import time

        out_dir = os.path.join(os.path.dirname(__file__), "failed")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{int(time.time() * 1000)}.png")
        with open(path, "wb") as f:
            f.write(image_bytes)
    except Exception as e:
        print(f"Could not save failed CAPTCHA image: {e}")
def _call_claude_captcha_api(b64: str, api_key: str):
    """One request to the Claude vision CAPTCHA endpoint. Returns the requests.Response."""
    import requests
    return requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 50,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64
                        }
                    },
                    {
                        "type": "text",
                        "text": "This is a CAPTCHA image. Read the text shown and reply with ONLY the characters you see, nothing else. No spaces, no explanation."
                    }
                ]
            }]
        },
        timeout=30
    )

def solve_captcha_claude(image_bytes: bytes) -> str:
    """Use Claude vision to read CAPTCHA."""
    import base64
    import os
    import re
    import time

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""

    b64 = base64.b64encode(image_bytes).decode()

    for attempt in range(2):  # one retry on a transient API error
        try:
            response = _call_claude_captcha_api(b64, api_key)

            if response.status_code != 200:
                is_transient = response.status_code in (429, 529)
                print(f"Claude CAPTCHA solver failed: HTTP {response.status_code} - {response.text[:200]}")
                if is_transient and attempt == 0:
                    time.sleep(3)
                    continue
                return ""

            result = response.json()["content"][0]["text"].strip()
            result = re.sub(r'[^A-Za-z0-9]', '', result)
            print(f"Claude solved CAPTCHA: {result}")
            return result.upper()

        except Exception as e:
            print(f"Claude CAPTCHA solver failed: {e}")
            return ""

    return ""