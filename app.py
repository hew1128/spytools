from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import sqlite3, json, re, os, time, random
from datetime import datetime, date, timezone, timedelta
import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))

def today_kst():
    return datetime.now(KST).strftime('%Y-%m-%d')

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
        group_name TEXT DEFAULT '기타',
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    try:
        c.execute("ALTER TABLE products ADD COLUMN group_name TEXT DEFAULT '기타'")
    except Exception:
        pass
    c.execute('''CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        review_count INTEGER,
        rating REAL,
        price INTEGER,
        purchase_count INTEGER,
        wishlist_count INTEGER,
        error TEXT,
        collected_at TEXT NOT NULL,
        FOREIGN KEY (product_id) REFERENCES products(id)
    )''')
    for col_def in ['purchase_count INTEGER', 'wishlist_count INTEGER', 'organic_rank INTEGER']:
        try:
            c.execute(f"ALTER TABLE snapshots ADD COLUMN {col_def}")
        except Exception:
            pass
    try:
        c.execute("ALTER TABLE products ADD COLUMN registered_date TEXT")
    except Exception:
        pass
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def get_all_groups():
    stored = get_setting('groups')
    if stored:
        try:
            return json.loads(stored)
        except Exception:
            pass
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT group_name FROM products WHERE is_active=1 AND group_name IS NOT NULL"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows] if rows else ['기타']


def save_groups(groups):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('groups',?)",
                 (json.dumps(groups),))
    conn.commit()
    conn.close()


def sort_groups(group_keys):
    all_groups = get_all_groups()
    order_map = {name: i for i, name in enumerate(all_groups)}
    return sorted(group_keys, key=lambda g: (order_map.get(g, 9999), g))

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
            title = soup.find('title')
            snippet = content[:200].replace('\n', ' ')
            result['error'] = f'NO_NEXT_DATA title={title.text[:40] if title else "없음"} | html={snippet}'
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
    today = today_kst()
    result = scrape_product(product)
    # 등록일은 products 테이블에 저장 (한 번만)
    if result.get('registered_date') and not product['registered_date']:
        conn.execute("UPDATE products SET registered_date=? WHERE id=?",
                     (result['registered_date'], pid))
    existing = conn.execute(
        "SELECT id FROM snapshots WHERE product_id=? AND collected_at=?", (pid, today)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE snapshots SET review_count=?,rating=?,price=?,purchase_count=?,wishlist_count=?,error=? WHERE product_id=? AND collected_at=?",
            (result.get('review_count'), result.get('rating'), result.get('price'),
             result.get('purchase_count'), result.get('wishlist_count'),
             result.get('error'), pid, today)
        )
    else:
        conn.execute(
            "INSERT INTO snapshots (product_id,review_count,rating,price,purchase_count,wishlist_count,error,collected_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, result.get('review_count'), result.get('rating'), result.get('price'),
             result.get('purchase_count'), result.get('wishlist_count'),
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
        today = today_kst()
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

    # build flat item list
    flat = []
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
        chart_rows = conn.execute(
            "SELECT collected_at, review_count FROM snapshots WHERE product_id=? AND review_count IS NOT NULL ORDER BY collected_at ASC LIMIT 365",
            (p['id'],)
        ).fetchall()
        chart_snaps = [{'date': r['collected_at'], 'count': r['review_count']} for r in chart_rows]
        flat.append({'product': p, 'latest': latest, 'prev': prev,
                     'review_diff': review_diff, 'chart_snaps': chart_snaps})

    prod_map = {}
    for item in flat:
        gname = item['product']['group_name'] or '기타'
        prod_map.setdefault(gname, []).append(item)

    prod_order_stored = get_setting('product_order')
    prod_order = {}
    if prod_order_stored:
        try:
            for i, pid in enumerate(json.loads(prod_order_stored)):
                prod_order[pid] = i
        except Exception:
            pass
    for gname in prod_map:
        prod_map[gname].sort(key=lambda x: prod_order.get(x['product']['id'], 9999))

    all_groups = get_all_groups()
    for gname in prod_map:
        if gname not in all_groups:
            all_groups.append(gname)
    sorted_keys = sort_groups(all_groups)
    groups = [(gname, prod_map.get(gname, [])) for gname in sorted_keys]

    conn.close()
    return render_template('dashboard.html', groups=groups)


@app.route('/add', methods=['GET', 'POST'])
def add_product():
    existing_groups = get_all_groups()
    if request.method == 'POST':
        name       = request.form.get('name', '').strip()
        url        = request.form.get('url', '').strip()
        platform   = request.form.get('platform', 'naver')
        memo       = request.form.get('memo', '').strip()
        group_name = request.form.get('group_name', '기타').strip() or '기타'
        if not name or not url:
            flash('상품명과 URL을 입력해주세요.')
            return render_template('add.html', existing_groups=existing_groups)
        conn = get_db()
        conn.execute(
            "INSERT INTO products (name,url,platform,memo,group_name) VALUES (?,?,?,?,?)",
            (name, url, platform, memo, group_name)
        )
        conn.commit()
        conn.close()
        flash(f'"{name}" 등록 완료.')
        return redirect(url_for('dashboard'))
    return render_template('add.html', existing_groups=existing_groups)


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
    existing_groups = get_all_groups()
    if request.method == 'POST':
        name       = request.form.get('name', '').strip()
        url        = request.form.get('url', '').strip()
        platform   = request.form.get('platform', 'naver')
        memo       = request.form.get('memo', '').strip()
        group_name = request.form.get('group_name', '기타').strip() or '기타'
        conn.execute(
            "UPDATE products SET name=?,url=?,platform=?,memo=?,group_name=? WHERE id=?",
            (name, url, platform, memo, group_name, pid)
        )
        conn.commit()
        conn.close()
        flash('수정 완료')
        return redirect(url_for('product_detail', pid=pid))
    conn.close()
    return render_template('edit.html', product=product, existing_groups=existing_groups)


@app.route('/group-order', methods=['POST'])
def group_order():
    order = request.get_json()
    if not isinstance(order, list):
        return jsonify({'ok': False}), 400
    save_groups(order)
    return jsonify({'ok': True})


@app.route('/product-order', methods=['POST'])
def product_order():
    order = request.get_json()
    if not isinstance(order, list):
        return jsonify({'ok': False}), 400
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('product_order',?)",
                 (json.dumps(order),))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/group/create', methods=['POST'])
def group_create():
    name = (request.get_json() or {}).get('name', '').strip()
    if not name:
        return jsonify({'ok': False, 'error': '이름 없음'})
    groups = get_all_groups()
    if name not in groups:
        groups.append(name)
        save_groups(groups)
    return jsonify({'ok': True})


@app.route('/group/delete', methods=['POST'])
def group_delete_route():
    name = (request.get_json() or {}).get('name', '').strip()
    if not name:
        return jsonify({'ok': False, 'error': '이름 없음'})
    groups = get_all_groups()
    if len(groups) <= 1:
        return jsonify({'ok': False, 'error': '그룹이 1개뿐이라 삭제할 수 없습니다'})
    if name in groups:
        groups.remove(name)
    fallback = groups[0] if groups else '기타'
    conn = get_db()
    conn.execute("UPDATE products SET group_name=? WHERE group_name=? AND is_active=1", (fallback, name))
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('groups',?)",
                 (json.dumps(groups),))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/product/<int:pid>/group', methods=['POST'])
def move_product_group(pid):
    group = (request.get_json() or {}).get('group', '기타').strip() or '기타'
    conn = get_db()
    conn.execute("UPDATE products SET group_name=? WHERE id=?", (group, pid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/product/<int:pid>/delete', methods=['POST'])
def delete_product(pid):
    conn = get_db()
    conn.execute("UPDATE products SET is_active=0 WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    flash('삭제 완료')
    return redirect(url_for('dashboard'))


# ── 로컬 수집기용 API ─────────────────────────────────────────────

@app.route('/api/products', methods=['GET'])
def api_products():
    conn = get_db()
    rows = conn.execute("SELECT id, name, url, platform, memo FROM products WHERE is_active=1").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/push', methods=['POST'])
def api_push():
    data = request.get_json()
    if not data or not isinstance(data, list):
        return jsonify({'ok': False, 'error': 'invalid data'}), 400
    conn = get_db()
    count = 0
    for item in data:
        pid            = item.get('product_id')
        review_count   = item.get('review_count')
        rating         = item.get('rating')
        price          = item.get('price')
        purchase_count = item.get('purchase_count')
        wishlist_count = item.get('wishlist_count')
        organic_rank   = item.get('organic_rank')
        registered_date= item.get('registered_date')
        error          = item.get('error')
        today          = item.get('date') or today_kst()
        if not pid:
            continue
        # 등록일은 products 테이블에 한 번만 저장
        if registered_date:
            p = conn.execute("SELECT registered_date FROM products WHERE id=?", (pid,)).fetchone()
            if p and not p['registered_date']:
                conn.execute("UPDATE products SET registered_date=? WHERE id=?", (registered_date, pid))
        existing = conn.execute(
            "SELECT id FROM snapshots WHERE product_id=? AND collected_at=?", (pid, today)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE snapshots SET review_count=?,rating=?,price=?,purchase_count=?,wishlist_count=?,organic_rank=?,error=? WHERE product_id=? AND collected_at=?",
                (review_count, rating, price, purchase_count, wishlist_count, organic_rank, error, pid, today)
            )
        else:
            conn.execute(
                "INSERT INTO snapshots (product_id,review_count,rating,price,purchase_count,wishlist_count,organic_rank,error,collected_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, review_count, rating, price, purchase_count, wishlist_count, organic_rank, error, today)
            )
        count += 1
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'saved': count})


if __name__ == '__main__':
    app.run(debug=True)
