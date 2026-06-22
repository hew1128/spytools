from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import sqlite3, json, re, os, time, random
from datetime import datetime, date
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
app.secret_key = 'spykey2024'

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'spy.db'))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        platform TEXT DEFAULT 'naver',
        memo TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        review_count INTEGER,
        rating REAL,
        price INTEGER,
        error TEXT,
        collected_at TEXT NOT NULL,
        FOREIGN KEY (product_id) REFERENCES products(id)
    )''')
    conn.commit()
    conn.close()

init_db()

# ── 스크래퍼 ────────────────────────────────────────────────────

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ko-KR,ko;q=0.9',
    'Connection': 'keep-alive',
}

def scrape_naver(url):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                      '--disable-setuid-sandbox', '--single-process']
            )
            page = browser.new_page(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                locale='ko-KR',
            )
            page.set_extra_http_headers({'Accept-Language': 'ko-KR,ko;q=0.9'})
            page.goto(url, wait_until='networkidle', timeout=45000)
            page.wait_for_timeout(3000)
            content = page.content()
            browser.close()

        soup = BeautifulSoup(content, 'html.parser')
        result = {'review_count': None, 'rating': None, 'price': None, 'error': None}

        next_script = soup.find('script', {'id': '__NEXT_DATA__'})
        if not next_script or not next_script.string:
            # __NEXT_DATA__ 없음 → 로그인 요구 또는 봇 차단 페이지
            title = soup.find('title')
            result['error'] = f'NO_NEXT_DATA (title={title.text[:60] if title else "?"})'
            return result

        s = next_script.string
        m = re.search(r'"reviewCount"\s*:\s*(\d+)', s)
        if m: result['review_count'] = int(m.group(1))
        m = re.search(r'"averageRating"\s*:\s*([\d.]+)', s)
        if m: result['rating'] = float(m.group(1))
        for pat in [r'"salePrice"\s*:\s*(\d{3,7})', r'"discountedSalePrice"\s*:\s*(\d{3,7})',
                    r'"price"\s*:\s*(\d{3,7})']:
            m = re.search(pat, s)
            if m:
                result['price'] = int(m.group(1))
                break

        if result['review_count'] is None:
            # reviewCount 키가 없으면 다른 키 시도
            for pat in [r'"totalReviewCount"\s*:\s*(\d+)', r'"reviewTotalCount"\s*:\s*(\d+)']:
                m = re.search(pat, s)
                if m:
                    result['review_count'] = int(m.group(1))
                    break

        return result
    except Exception as e:
        return {'review_count': None, 'rating': None, 'price': None, 'error': str(e)[:300]}


def scrape_coupang(url):
    try:
        headers = dict(HEADERS)
        headers['Referer'] = 'https://www.coupang.com/'
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        result = {'review_count': None, 'rating': None, 'price': None, 'error': None}

        # 리뷰 수
        for sel in ['.rating-total-count', '[class*="rating-total"]',
                    '#btfTab .js-sidebar-review-count']:
            el = soup.select_one(sel)
            if el:
                m = re.search(r'[\d,]+', el.get_text())
                if m:
                    result['review_count'] = int(m.group().replace(',', ''))
                    break

        # 평점
        el = soup.select_one('.rating-star-num')
        if el:
            m = re.search(r'[\d.]+', el.get_text())
            if m: result['rating'] = float(m.group())

        # 가격
        for sel in ['.prod-buy-price .total-price strong',
                    '[class*="prod-price"] strong']:
            el = soup.select_one(sel)
            if el:
                m = re.search(r'[\d,]+', el.get_text())
                if m:
                    result['price'] = int(m.group().replace(',', ''))
                    break

        return result
    except Exception as e:
        return {'review_count': None, 'rating': None, 'price': None, 'error': str(e)[:300]}


def scrape_product(product):
    if product['platform'] == 'naver':
        return scrape_naver(product['url'])
    elif product['platform'] == 'coupang':
        return scrape_coupang(product['url'])
    return {'review_count': None, 'rating': None, 'price': None,
            'error': f'지원하지 않는 플랫폼: {product["platform"]}'}


def do_collect(pid, conn):
    product = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not product:
        return
    today = date.today().isoformat()
    result = scrape_product(product)
    existing = conn.execute(
        "SELECT id FROM snapshots WHERE product_id=? AND collected_at=?", (pid, today)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE snapshots SET review_count=?,rating=?,price=?,error=? WHERE product_id=? AND collected_at=?",
            (result.get('review_count'), result.get('rating'), result.get('price'),
             result.get('error'), pid, today)
        )
    else:
        conn.execute(
            "INSERT INTO snapshots (product_id,review_count,rating,price,error,collected_at) VALUES (?,?,?,?,?,?)",
            (pid, result.get('review_count'), result.get('rating'), result.get('price'),
             result.get('error'), today)
        )
    conn.commit()
    return result


# ── 스케줄러 (매일 오전 7시 자동 수집) ──────────────────────────
try:
    from apscheduler.schedulers.background import BackgroundScheduler

    def scheduled_collect():
        conn = get_db()
        products = conn.execute("SELECT * FROM products WHERE is_active=1").fetchall()
        today = date.today().isoformat()
        for p in products:
            ex = conn.execute(
                "SELECT id FROM snapshots WHERE product_id=? AND collected_at=?",
                (p['id'], today)
            ).fetchone()
            if not ex:
                do_collect(p['id'], conn)
        conn.close()

    scheduler = BackgroundScheduler(timezone='Asia/Seoul')
    scheduler.add_job(scheduled_collect, 'cron', hour=7, minute=0)
    scheduler.start()
except Exception:
    pass


# ── 라우트 ────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    conn = get_db()
    products = conn.execute(
        "SELECT * FROM products WHERE is_active=1 ORDER BY id"
    ).fetchall()
    items = []
    for p in products:
        snaps = conn.execute(
            "SELECT * FROM snapshots WHERE product_id=? ORDER BY collected_at DESC LIMIT 2",
            (p['id'],)
        ).fetchall()
        latest = snaps[0] if snaps else None
        prev   = snaps[1] if len(snaps) > 1 else None

        review_diff = None
        if latest and prev:
            if latest['review_count'] is not None and prev['review_count'] is not None:
                review_diff = latest['review_count'] - prev['review_count']

        items.append({'product': p, 'latest': latest, 'prev': prev, 'review_diff': review_diff})
    conn.close()
    return render_template('dashboard.html', items=items)


@app.route('/add', methods=['GET', 'POST'])
def add_product():
    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        url      = request.form.get('url', '').strip()
        platform = request.form.get('platform', 'naver')
        memo     = request.form.get('memo', '').strip()
        if not name or not url:
            flash('상품명과 URL을 입력해주세요.')
            return render_template('add.html')
        conn = get_db()
        conn.execute(
            "INSERT INTO products (name,url,platform,memo) VALUES (?,?,?,?)",
            (name, url, platform, memo)
        )
        conn.commit()
        conn.close()
        flash(f'"{name}" 등록 완료.')
        return redirect(url_for('dashboard'))
    return render_template('add.html')


@app.route('/product/<int:pid>')
def product_detail(pid):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not product:
        return redirect(url_for('dashboard'))
    snaps = conn.execute(
        "SELECT * FROM snapshots WHERE product_id=? ORDER BY collected_at DESC LIMIT 90",
        (pid,)
    ).fetchall()
    conn.close()

    rows = []
    for i, s in enumerate(snaps):
        prev = snaps[i + 1] if i + 1 < len(snaps) else None
        review_diff = price_diff = None
        if prev:
            if s['review_count'] is not None and prev['review_count'] is not None:
                review_diff = s['review_count'] - prev['review_count']
            if s['price'] is not None and prev['price'] is not None:
                price_diff = s['price'] - prev['price']
        rows.append({'snap': s, 'review_diff': review_diff, 'price_diff': price_diff})

    return render_template('product.html', product=product, rows=rows)


@app.route('/collect/<int:pid>', methods=['POST'])
def collect_one(pid):
    conn = get_db()
    result = do_collect(pid, conn)
    conn.close()
    if result:
        return jsonify({'ok': True, 'result': result})
    return jsonify({'ok': False})


@app.route('/collect-all', methods=['POST'])
def collect_all_route():
    conn = get_db()
    products = conn.execute("SELECT * FROM products WHERE is_active=1").fetchall()
    for p in products:
        do_collect(p['id'], conn)
    conn.close()
    flash('전체 수집 완료')
    return redirect(url_for('dashboard'))


@app.route('/product/<int:pid>/edit', methods=['GET', 'POST'])
def edit_product(pid):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not product:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        url      = request.form.get('url', '').strip()
        platform = request.form.get('platform', 'naver')
        memo     = request.form.get('memo', '').strip()
        conn.execute(
            "UPDATE products SET name=?,url=?,platform=?,memo=? WHERE id=?",
            (name, url, platform, memo, pid)
        )
        conn.commit()
        conn.close()
        flash('수정 완료')
        return redirect(url_for('product_detail', pid=pid))
    conn.close()
    return render_template('edit.html', product=product)


@app.route('/product/<int:pid>/delete', methods=['POST'])
def delete_product(pid):
    conn = get_db()
    conn.execute("UPDATE products SET is_active=0 WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    flash('삭제 완료')
    return redirect(url_for('dashboard'))


if __name__ == '__main__':
    app.run(debug=True)
