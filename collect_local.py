"""
염탐기 로컬 수집 스크립트
- setting.json 에서 네이버 ID/PW 읽어서 자동 로그인
- 수집 결과를 Railway 서버에 전송
- 실행: python collect_local.py
"""

import re
import json
import time
import os
import urllib.parse
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ── 설정 ──────────────────────────────────────────────────────────
SERVER_URL = 'https://web-production-54ce2d.up.railway.app'
SETTING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'setting.json')


def load_setting():
    with open(SETTING_FILE, encoding='utf-8') as f:
        return json.load(f)


def naver_login(page, naver_id, naver_pw):
    print('  네이버 로그인 중...')
    page.goto('https://nid.naver.com/nidlogin.login', wait_until='domcontentloaded', timeout=20000)
    page.wait_for_timeout(1500)
    page.click('#id')
    page.keyboard.type(naver_id, delay=80)
    page.wait_for_timeout(500)
    page.click('#pw')
    page.keyboard.type(naver_pw, delay=80)
    page.wait_for_timeout(500)
    page.click('.btn_login')
    page.wait_for_timeout(3000)
    if 'nidlogin' not in page.url:
        print('  로그인 성공!')
        return True
    # 캡챠 등 추가 인증 - 브라우저 창에서 직접 로그인 완료 기다림 (최대 120초)
    print('  [브라우저 창에서 로그인 완료해주세요] 자동으로 감지합니다...')
    for _ in range(60):
        page.wait_for_timeout(2000)
        if 'nidlogin' not in page.url and 'naver.com' in page.url:
            print('  로그인 성공!')
            return True
    print('  로그인 시간 초과. 계속 진행합니다.')
    return False


def parse_korean_number(text):
    """'1.5만' → 15000, '6,690' → 6690"""
    if not text:
        return None
    text = text.strip().replace(',', '')
    m = re.search(r'([\d.]+)(만|천)?', text)
    if not m:
        return None
    num = float(m.group(1))
    if m.group(2) == '만':
        num *= 10000
    elif m.group(2) == '천':
        num *= 1000
    return int(num)


def search_naver_shopping(product_name, store_hint, page):
    """네이버 쇼핑 검색 후 일반(비광고) 카드에서 구매수/찜수/등록일/유기순위 수집"""
    result = {'purchase_count': None, 'wishlist_count': None,
              'registered_date': None, 'organic_rank': None}
    if not store_hint or not store_hint.strip():
        return result
    try:
        query = urllib.parse.quote(product_name)
        page.goto(
            f'https://search.shopping.naver.com/search/all?query={query}',
            wait_until='domcontentloaded', timeout=30000
        )
        page.wait_for_timeout(2000)

        hint = store_hint.strip().lower()
        data = page.evaluate(f'''() => {{
            const hint = {json.dumps(hint)};
            // 광고 제외 — 일반 카드만 (product_item__K0ayS)
            const cards = document.querySelectorAll('div.product_item__K0ayS');
            for (const card of cards) {{
                const cardText = card.innerText.toLowerCase();
                if (!cardText.includes(hint)) continue;

                // 구매수
                let purchase = null;
                const purchaseEl = card.querySelector('[data-shp-area*="purchasecount"] .product_num__WuH26, [data-shp-area*="purchasecount"] em');
                if (purchaseEl) purchase = purchaseEl.innerText.trim();

                // 찜수 / 등록일: span.product_etc__Z7jnS 중 텍스트로 구분
                let wish = null, regDate = null;
                const etcSpans = card.querySelectorAll('[class*="product_etc__"]');
                for (const sp of etcSpans) {{
                    const t = sp.innerText.trim();
                    if (t.startsWith('찜')) {{
                        const numEl = sp.querySelector('[class*="product_num__"]');
                        if (numEl) wish = numEl.innerText.trim();
                    }} else if (t.startsWith('등록일')) {{
                        const m = t.match(/(\d{{4}}\.\d{{1,2}}\.?)/);
                        if (m) regDate = m[1].replace(/\.$/, '');
                    }}
                }}

                // 유기 순위: data-shp-contents-dt 속성에서 organic_expose_order
                let organicRank = null;
                const rankEl = card.querySelector('[data-shp-contents-dt]');
                if (rankEl) {{
                    try {{
                        const dt = JSON.parse(rankEl.getAttribute('data-shp-contents-dt') || '[]');
                        const oe = dt.find(x => x.key === 'organic_expose_order');
                        if (oe) organicRank = parseInt(oe.value);
                    }} catch(e) {{}}
                }}

                return {{ purchase, wish, regDate, organicRank }};
            }}
            return null;
        }}''')

        if data:
            result['purchase_count'] = parse_korean_number(data.get('purchase'))
            result['wishlist_count']  = parse_korean_number(data.get('wish'))
            result['registered_date'] = data.get('regDate')
            if data.get('organicRank') is not None:
                result['organic_rank'] = data['organicRank']
    except Exception as e:
        print(f'    쇼핑 검색 오류: {str(e)[:80]}')
    return result


def scrape_naver(url, page):
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        result = {'review_count': None, 'rating': None, 'price': None, 'error': None}

        # 리뷰 수: "38,988건 리뷰" 형태
        try:
            el = page.wait_for_selector('[data-shp-area="sprvsub.rvmore"]', timeout=8000)
            text = el.inner_text()
            m = re.search(r'[\d,]+', text)
            if m:
                result['review_count'] = int(m.group().replace(',', ''))
        except Exception:
            pass

        # 가격: 네이버 스마트스토어 공통 패턴
        for sel in ['[class*="price"] strong', '[class*="salePrice"]',
                    'span[class*="price"] em', 'strong[class*="price"]']:
            try:
                el = page.query_selector(sel)
                if el:
                    text = el.inner_text().replace(',', '').strip()
                    m = re.search(r'\d{3,7}', text)
                    if m:
                        result['price'] = int(m.group())
                        break
            except Exception:
                pass

        if result['review_count'] is None and result['price'] is None:
            result['error'] = '데이터 없음 (페이지 구조 변경 가능성)'
        return result
    except Exception as e:
        return {'review_count': None, 'rating': None, 'price': None, 'error': str(e)[:200]}


def scrape_coupang(url, page):
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(3000)
        content = page.content()
        soup = BeautifulSoup(content, 'html.parser')
        result = {'review_count': None, 'rating': None, 'price': None, 'error': None}

        for sel in ['.rating-total-count', '[class*="rating-total"]']:
            el = soup.select_one(sel)
            if el:
                m = re.search(r'[\d,]+', el.get_text())
                if m:
                    result['review_count'] = int(m.group().replace(',', ''))
                    break
        el = soup.select_one('.rating-star-num')
        if el:
            m = re.search(r'[\d.]+', el.get_text())
            if m: result['rating'] = float(m.group())
        for sel in ['.prod-buy-price .total-price strong']:
            el = soup.select_one(sel)
            if el:
                m = re.search(r'[\d,]+', el.get_text())
                if m:
                    result['price'] = int(m.group().replace(',', ''))
                    break
        return result
    except Exception as e:
        return {'review_count': None, 'rating': None, 'price': None, 'error': str(e)[:200]}


def main():
    print('[염탐기 로컬 수집기]')
    print(f'서버: {SERVER_URL}\n')

    # setting.json 읽기
    try:
        setting = load_setting()
        naver_id = setting.get('네이버_id', '')
        naver_pw = setting.get('네이버_pw', '')
    except Exception as e:
        print(f'setting.json 읽기 실패: {e}')
        naver_id = naver_pw = ''

    # 상품 목록 가져오기
    try:
        resp = requests.get(f'{SERVER_URL}/api/products', timeout=10)
        products = resp.json()
    except Exception as e:
        print(f'서버 연결 실패: {e}')
        return

    if not products:
        print('수집할 상품이 없습니다.')
        return

    print(f'상품 {len(products)}개 수집 시작...\n')

    # 수집기와 동일하게 별도 세션 폴더 사용 (Chrome 안 닫아도 됨)
    SESSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'naver_session')
    os.makedirs(SESSION_DIR, exist_ok=True)

    results = []
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=False,
            no_viewport=True,
            args=['--disable-blink-features=AutomationControlled'],
        )
        page = context.new_page()

        # 세션 없으면 로그인 (첫 실행 시 1회만)
        has_naver = any(prod['platform'] == 'naver' for prod in products)
        if has_naver:
            page.goto('https://www.naver.com', wait_until='domcontentloaded', timeout=15000)
            page.wait_for_timeout(1000)
            if 'naver.com' in page.url and page.query_selector('input#id') is None:
                print('  세션 유지 중 (로그인 생략)')
            elif naver_id:
                naver_login(page, naver_id, naver_pw)

        for prod in products:
            pid      = prod['id']
            name     = prod['name']
            url      = prod['url']
            platform = prod['platform']
            print(f'  수집: {name[:35]}')

            if platform == 'naver':
                r = scrape_naver(url, page)
                # 네이버 쇼핑 검색으로 구매수/찜수/등록일/유기순위 추가 수집
                memo = prod.get('memo', '') or ''
                if memo.strip():
                    print(f'    쇼핑 검색 중 (힌트: {memo[:20]})...')
                    shopping = search_naver_shopping(prod['name'], memo, page)
                    r.update({k: v for k, v in shopping.items() if v is not None})
            elif platform == 'coupang':
                r = scrape_coupang(url, page)
            else:
                r = {'review_count': None, 'rating': None, 'price': None, 'error': f'지원안함:{platform}'}

            r['product_id'] = pid
            r['date'] = datetime.now().strftime('%Y-%m-%d')
            results.append(r)
            parts = []
            if r.get('review_count') is not None: parts.append(f"리뷰:{r['review_count']}")
            if r.get('purchase_count') is not None: parts.append(f"구매:{r['purchase_count']}")
            if r.get('wishlist_count') is not None: parts.append(f"찜:{r['wishlist_count']}")
            if r.get('organic_rank') is not None: parts.append(f"유기순위:{r['organic_rank']}")
            if r.get('error'): parts.append(f"오류:{r['error'][:30]}")
            print(f'    → {" | ".join(parts) if parts else "데이터없음"}')
            time.sleep(1)

        context.close()

    # 서버로 전송
    print(f'\n서버에 결과 전송 중...')
    try:
        resp = requests.post(
            f'{SERVER_URL}/api/push',
            json=results,
            headers={'Content-Type': 'application/json'},
            timeout=15
        )
        data = resp.json()
        print(f'완료: {data.get("saved", 0)}개 저장')
    except Exception as e:
        print(f'전송 실패: {e}')

    print(f'\n수집 완료! 브라우저에서 확인: {SERVER_URL}')


if __name__ == '__main__':
    main()
