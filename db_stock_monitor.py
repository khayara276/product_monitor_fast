import json
import time
import random
import threading
import queue
import os
import requests 
import sqlite3
import concurrent.futures
import sys
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Check for stealth library
try:
    from curl_cffi import requests as crequests
except ImportError:
    print("❌ curl_cffi missing! Install it.")
    sys.exit(1)

from DrissionPage import ChromiumPage, ChromiumOptions

# ==========================================
# ⚙️ CONFIGURATION (ENV VARS)
# ==========================================
# GitHub Secrets se data uthayega
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
COOKIES_JSON = os.getenv('COOKIES_JSON') 

DB_PATH = os.path.join('data', 'shein_products.db')

# GHA par Headless zaroori hai
HEADLESS_MODE = True 

# PERFORMANCE TUNING (GitHub Runners have 7GB RAM! 🚀)
NUM_THREADS = 50        # Wapis high speed threads
BATCH_SIZE = 50         
CYCLE_DELAY = 0.5       

# Priority Sizes
PRIORITY_SIZES = ["XS", "S", "M", "L", "XL", "XXL", "28", "30", "32", "34", "36"]

# ==========================================
# 🛠️ UTILITY FUNCTIONS
# ==========================================

def send_telegram(message, image_url=None, button_url=None):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        payload = {
            "chat_id": CHAT_ID,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        if button_url:
            payload["reply_markup"] = json.dumps({
                "inline_keyboard": [[{"text": "🛍️ BUY NOW", "url": button_url}]]
            })
        if image_url:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            payload["photo"] = image_url
            payload["caption"] = message
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload["text"] = message
        requests.post(url, data=payload, timeout=10)
    except Exception: pass

# ==========================================
# 🚀 DB MONITOR CLASS (GHA EDITION)
# ==========================================

class DBStockMonitor:
    def __init__(self):
        self.browser = None
        self.running = True
        self.batch_queue = queue.Queue()
        self.stock_cache = {} 
        self.ignore_list = set() 
        self.total_products = 0
        self.processed_count = 0
        self.lock = threading.Lock()
        
        # Start with Stealth Mode
        self.use_python_mode = True 
        
        # Use chrome120 for better stealth
        self.session = crequests.Session(impersonate="chrome120")
        self.setup_session()
        
        self.init_database()

    def setup_session(self):
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Referer': 'https://sheinindia.ajio.com/',
            'Origin': 'https://sheinindia.ajio.com'
        })

    def init_database(self):
        if not os.path.exists('data'): os.makedirs('data')
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id TEXT PRIMARY KEY,
                product_name TEXT,
                price INTEGER,
                url TEXT,
                image_url TEXT,
                last_updated TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def get_clean_image_url(self, raw_url):
        if not raw_url: return None
        try:
            clean_url = re.sub(r'_\d+x\d+', '', raw_url).replace('_thumbnail', '')
            if 'ajio.com' in clean_url: clean_url = clean_url.split('?')[0]
            return clean_url
        except: return raw_url

    def get_browser_options(self, port):
        co = ChromiumOptions()
        co.set_local_port(port)
        co.set_argument('--no-sandbox')
        co.set_argument('--headless') 
        co.set_argument('--disable-gpu')
        co.set_argument('--disable-dev-shm-usage')
        co.set_argument('--blink-settings=imagesEnabled=false')
        return co

    def init_browser(self):
        if self.browser: return True
        
        print("🚀 Initializing Browser...", flush=True)
        try:
            port = random.randint(40000, 50000)
            co = self.get_browser_options(port)
            self.browser = ChromiumPage(co)
            BASE_URL = 'https://sheinindia.ajio.com/'
            
            if COOKIES_JSON:
                try:
                    cookies_list = json.loads(COOKIES_JSON)
                    self.browser.get(BASE_URL)
                    self.browser.set.cookies(cookies_list)
                    self.browser.refresh()
                    print("✅ Cookies injected.", flush=True)
                except Exception as e:
                    print(f"⚠️ Cookie Error: {e}", flush=True)
            
            self.sync_cookies()
            return True
        except Exception as e:
            print(f"❌ Browser Init Failed: {e}", flush=True)
            return False

    def sync_cookies(self):
        print("🔄 Syncing Session...", flush=True)
        try:
            cookies = self.browser.cookies()
            cookies_dict = {c['name']: c['value'] for c in cookies}
            self.session.cookies.update(cookies_dict)
            
            ua = self.browser.run_js("return navigator.userAgent")
            if ua: self.session.headers.update({'User-Agent': ua})
            
            if 'A' in cookies_dict:
                self.use_python_mode = True
                print("   ✅ Auth Token Valid. Stealth Mode ON.", flush=True)
            else:
                print("   ⚠️ Auth Token Missing.", flush=True)
        except: pass

    def get_all_products(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT product_id, product_name FROM products")
        rows = cursor.fetchall()
        conn.close()
        return [row for row in rows if row[0] not in self.ignore_list]

    # --- HYBRID FETCHING ENGINE ---
    def hybrid_fetch_batch(self, pids):
        if self.use_python_mode:
            return self.fetch_batch_stealth(pids)
        else:
            return self.fetch_batch_browser(pids)

    def fetch_batch_stealth(self, pids):
        results = []
        forbidden_count = 0
        
        def fetch_one(pid):
            try:
                url = f"https://sheinindia.ajio.com/api/p/{pid}?fields=SITE"
                res = self.session.get(url, timeout=10)
                if res.status_code == 200:
                    return {'id': pid, 'status': 200, 'data': res.json()}
                elif res.status_code == 403:
                    return {'id': pid, 'status': 403}
                return {'id': pid, 'status': 'ERR'}
            except:
                return {'id': pid, 'status': 'ERR'}

        # GHA has good CPU, using 20 threads for internal batch
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(pids), 20)) as executor:
            future_to_pid = {executor.submit(fetch_one, pid): pid for pid in pids}
            for future in concurrent.futures.as_completed(future_to_pid):
                res = future.result()
                results.append(res)
                if res['status'] == 403: forbidden_count += 1

        if forbidden_count > 0:
            print(f"   ⚠️ 403 Detected. Switching to Browser...", flush=True)
            self.use_python_mode = False
            return self.fetch_batch_browser(pids)
        
        return results

    def fetch_batch_browser(self, pids):
        if not self.browser: self.init_browser()
        try:
            if random.random() < 0.1: self.sync_cookies() 
            pid_list_str = json.dumps(pids)
            js_code = f"""
                const pids = {pid_list_str};
                const requests = pids.map(pid => {{
                    return fetch(`https://sheinindia.ajio.com/api/p/${{pid}}?fields=SITE`, {{
                         headers: {{'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}}
                    }}).then(res => {{
                        if(res.status === 403) return {{id: pid, status: 403}};
                        if(!res.ok) return {{id: pid, status: 'ERR'}};
                        return res.json().then(data => ({{id: pid, status: 200, data: data}}));
                    }}).catch(e => ({{id: pid, status: 'ERR'}}));
                }});
                return Promise.all(requests).then(results => JSON.stringify(results));
            """
            result_str = self.browser.latest_tab.run_js(js_code, timeout=60)
            if result_str: return json.loads(result_str)
        except: pass
        return []

    def worker_loop(self):
        while self.running:
            try:
                try:
                    batch_items = self.batch_queue.get(timeout=1)
                except queue.Empty:
                    continue 

                try:
                    pids = [item[0] for item in batch_items]
                    name_map = {item[0]: item[1] for item in batch_items}
                    
                    results = self.hybrid_fetch_batch(pids)
                    
                    if results:
                        for res in results:
                            if res.get('status') == 200 and res.get('data'):
                                self.process_product(res['id'], res['data'], name_map.get(res['id']))
                    
                    with self.lock:
                        self.processed_count += len(batch_items)
                        pct = (self.processed_count / self.total_products) * 100 if self.total_products else 0
                        mode_icon = "🚀" if self.use_python_mode else "🐢"
                        if self.processed_count % 500 < BATCH_SIZE:
                            print(f"   {mode_icon} Progress: {self.processed_count}/{self.total_products} ({pct:.1f}%)", end='\r', flush=True)

                finally:
                    self.batch_queue.task_done()
                    time.sleep(0.01) # Very small delay for speed

            except Exception: pass

    def process_product(self, pid, data, old_name):
        try:
            if pid in self.ignore_list: return

            name = data.get('productRelationID', data.get('name', old_name or 'Unknown'))
            price_obj = data.get('offerPrice') or data.get('price')
            price_val = f"₹{int(price_obj.get('value'))}" if price_obj and price_obj.get('value') else "N/A"

            current_stock = {}
            for v in data.get('variantOptions', []):
                qs = v.get('variantOptionQualifiers', [])
                size = next((q['value'] for q in qs if q['qualifier'] == 'size'), 
                       next((q['value'] for q in qs if q['qualifier'] == 'standardSize'), None))
                if size:
                    status = v.get('stock', {}).get('stockLevelStatus', '')
                    qty = v.get('stock', {}).get('stockLevel', 0)
                    if status in ['inStock', 'lowStock']: current_stock[size] = qty
                    else: current_stock[size] = 0

            # Alert Logic
            current_sig = ",".join([f"{k}:{v}" for k, v in sorted(current_stock.items())])
            last_sig = self.stock_cache.get(pid)
            
            found_priority = []
            for size, qty in current_stock.items():
                if qty > 0 and size.upper() in PRIORITY_SIZES:
                    found_priority.append(f"{size} ({qty})")

            should_alert = False
            status_msg = ""
            
            if current_sig != last_sig:
                if found_priority:
                    if not last_sig: 
                        status_msg = f"🔥 <b>TRACKING STARTED</b>"
                        should_alert = True
                    else:
                        status_msg = f"⚡ <b>STOCK UPDATE</b>"
                        should_alert = True
                self.stock_cache[pid] = current_sig

            if should_alert:
                raw_img_url = data.get('selected', {}).get('modelImage', {}).get('url')
                if not raw_img_url and 'images' in data and data['images']: raw_img_url = data['images'][0].get('url')
                hd_img_url = self.get_clean_image_url(raw_img_url)
                buy_url = f"https://www.sheinindia.in/p/{pid}"
                
                priority_text = ", ".join(found_priority)
                all_sizes_text = "\n".join([f"{'✅' if q>0 else '❌'} {s}: {q}" for s, q in current_stock.items()])

                msg = (
                    f"{status_msg}\n\n"
                    f"👚 <b>{name}</b>\n"
                    f"💰 <b>{price_val}</b>\n\n"
                    f"🎯 <b>Priority Found:</b> {priority_text}\n\n"
                    f"📏 <b>Full Stock:</b>\n<pre>{all_sizes_text}</pre>\n\n"
                    f"🔗 <a href='{buy_url}'>Check on Shein</a>"
                )
                print(f"🔔 Alert Sent: {name}", flush=True)
                send_telegram(msg, image_url=hd_img_url, button_url=buy_url)
                self.ignore_list.add(pid)

        except Exception as e: pass

    def auto_cleaner(self):
        """Clears ignore list every 6 hours"""
        while self.running:
            wait_time = 6 * 3600 # 6 hours fixed for GHA
            print(f"\n⏰ Cleaner scheduled in 6 hours...", flush=True)
            time.sleep(wait_time)
            with self.lock:
                self.ignore_list.clear()
                self.stock_cache.clear()
                print(f"\n🧹 Cache Cleared!", flush=True)

    def start(self):
        print("="*60, flush=True)
        print("🚀 SHEIN MONITOR: GHA SPEED EDITION", flush=True)
        print(f"📦 Batch Size: {BATCH_SIZE} | Workers: {NUM_THREADS}", flush=True)
        print("="*60, flush=True)

        threading.Thread(target=self.auto_cleaner, daemon=True).start()

        # Init Browser & Sync
        self.init_browser()
        
        print(f"\n🧵 Launching {NUM_THREADS} Speed Workers...", flush=True)
        for _ in range(NUM_THREADS):
            t = threading.Thread(target=self.worker_loop, daemon=True)
            t.start()

        cycle = 0
        try:
            while True:
                cycle += 1
                all_products = self.get_all_products()
                self.total_products = len(all_products)
                self.processed_count = 0
                
                print(f"\n🔄 Cycle #{cycle}: Scanning {self.total_products} products...", flush=True)
                
                if self.total_products == 0:
                    print("⚠️ DB is empty.", flush=True)
                    time.sleep(60); continue

                for i in range(0, len(all_products), BATCH_SIZE):
                    batch = all_products[i:i + BATCH_SIZE]
                    self.batch_queue.put(batch)
                
                self.batch_queue.join()
                
                if not self.use_python_mode:
                    print("\n♻️  Restoring Stealth Mode...", flush=True)
                    self.init_browser()
                    self.sync_cookies()
                
                print(f"\n✅ Cycle Complete! Restarting...", flush=True)
                time.sleep(CYCLE_DELAY)
                
        except KeyboardInterrupt:
            self.running = False
            if self.browser: self.browser.quit()

if __name__ == "__main__":
    monitor = DBStockMonitor()
    monitor.start()
