"""
염탐기 로컬 수집 스크립트
- setting.json 에서 네이버 ID/PW 읽어서 자동 로그인
- 수집 결과를 Railway 서버에 전송
- 실행: python collect_local.py
"""

import re
import json
import time
import random
import os
import urllib.parse
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from patchright.sync_api import sync_playwright

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
    """네이버 쇼핑 검색 후 일반(비광고) 카드에서 리뷰수/별점/가격/구매수/찜수/등록일/유기순위 수집"""
    result = {'review_count': None, 'rating': None, 'price': None,
              'purchase_count': None, 'wishlist_count': None,
              'registered_date': None, 'organic_rank': None, 'error': None}
    if not store_hint or not store_hint.strip():
        return result
    try:
        # 네이버 메인 → 검색창 타이핑 → 쇼핑 탭 (직접 URL은 봇 감지로 캡챠 발생)
        page.goto('https://www.naver.com', wait_until='domcontentloaded', timeout=15000)
        page.wait_for_timeout(random.randint(400, 800))
        page.fill('input#query', product_name)
        page.wait_for_timeout(random.randint(200, 500))
        page.keyboard.press('Enter')
        page.wait_for_load_state('domcontentloaded', timeout=15000)
        page.wait_for_timeout(1200)

        # 쇼핑 탭 클릭 (여러 셀렉터 시도)
        shopped = False
        for sel in ['a[data-clk="sho"]', '#lnb a[href*="shopping"]', '#snb a[href*="shopping"]']:
            el = page.query_selector(sel)
            if el:
                el.click()
                page.wait_for_load_state('domcontentloaded', timeout=15000)
                shopped = True
                break
        if not shopped:
            # 쇼핑 탭을 못 찾으면 직접 URL 폴백
            query = urllib.parse.quote(product_name)
            page.goto(
                f'https://search.shopping.naver.com/search/all?query={query}&frm=NVSHATC',
                wait_until='load', timeout=30000
            )

        # 상품 카드 렌더링 대기
        try:
            page.wait_for_selector('[class*="product_"]', timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        hint = store_hint.strip().lower()
        data = page.evaluate(f'''() => {{
            const hint = {json.dumps(hint)};
            const allCards = Array.from(document.querySelectorAll('li[class*="product_"], div[class*="product_item"]'));
            const cards = allCards.filter(el =>
                !el.closest('[data-shp-ad-img]') && !el.closest('[class*="adProduct"]')
            );

            let matchedCard = null;
            for (const card of cards) {{
                if (card.innerText.toLowerCase().includes(hint)) {{
                    matchedCard = card;
                    break;
                }}
            }}

            if (!matchedCard) return {{ debug: 'no_match', cardCount: cards.length }};

            // 카드 전체 텍스트로 패턴 파싱 (★4.48 (1,358) · 구매 547 · 찜 377 · 등록일 2023.11.)
            const text = matchedCard.innerText;

            // 별점 + 리뷰수: ★4.48 (1,358)
            let rating = null, reviewCount = null;
            const starM = text.match(/★\s*([\d.]+)\s*\(([\d,]+)\)/);
            if (starM) {{ rating = starM[1]; reviewCount = starM[2].replace(/,/g, ''); }}

            // 구매수: 구매 547 또는 구매 1.5만
            let purchase = null;
            const purchM = text.match(/구매\s*([\d,.]+만?천?)/);
            if (purchM) purchase = purchM[1];

            // 찜수: 찜 377
            let wish = null;
            const wishM = text.match(/찜\s*([\d,.]+만?천?)/);
            if (wishM) wish = wishM[1];

            // 등록일: 등록일 2023.11.
            let regDate = null;
            const regM = text.match(/등록일\s*(\d{{4}}\.\d{{1,2}}\.?)/);
            if (regM) regDate = regM[1].replace(/\.$/, '');

            // 가격: price 관련 strong 태그
            let price = null;
            const priceEl = matchedCard.querySelector('[class*="price"] strong, strong[class*="Price"], [class*="price_"] strong');
            if (priceEl) {{
                const m = priceEl.innerText.replace(/,/g,'').match(/(\d{{3,7}})/);
                if (m) price = m[1];
            }}

            // 유기순위: data-shp-contents-dt → organic_expose_order
            let organicRank = null;
            const rankEl = matchedCard.querySelector('[data-shp-contents-dt]');
            if (rankEl) {{
                try {{
                    const dt = JSON.parse(rankEl.getAttribute('data-shp-contents-dt') || '[]');
                    const oe = dt.find(x => x.key === 'organic_expose_order');
                    if (oe) organicRank = parseInt(oe.value);
                }} catch(e) {{}}
            }}

            return {{ rating, reviewCount, purchase, wish, regDate, price, organicRank, debug: 'ok' }};
        }}''')

        if data:
            if data.get('debug') == 'no_match':
                print(f'    쇼핑: 카드 {data.get("cardCount", 0)}개 중 힌트 미매칭')
            else:
                result['review_count']    = parse_korean_number(data.get('reviewCount'))
                result['rating']          = float(data['rating']) if data.get('rating') else None
                result['price']           = int(data['price']) if data.get('price') else None
                result['purchase_count']  = parse_korean_number(data.get('purchase'))
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


def scrape_smartstore_keywords(page):
    """스마트스토어 마케팅분석 → 검색채널 키워드 수집"""
    results = []
    try:
        print('  [키워드] 스마트스토어 판매자센터 이동 중...')
        page.goto('https://sell.smartstore.naver.com/', wait_until='domcontentloaded', timeout=20000)
        page.wait_for_timeout(3000)

        # 로그인 여부 확인 — 로그인 안 됐으면 최대 3분 대기
        if 'sell.smartstore.naver.com' not in page.url or page.query_selector('input[type="password"]') is not None:
            print('  [키워드] 스마트스토어 로그인 필요 — 브라우저 창에서 로그인해주세요 (최대 3분)')
            for _ in range(90):
                page.wait_for_timeout(2000)
                url = page.url
                if 'sell.smartstore.naver.com' in url and 'login' not in url and 'nidlogin' not in url:
                    # 판매자센터 본 화면 진입 확인
                    if page.query_selector('[class*="gnb"], [class*="GNB"], nav') is not None:
                        print('  [키워드] 로그인 완료!')
                        break
            else:
                print('  [키워드] 로그인 시간 초과. 키워드 수집 건너뜀.')
                return results

        page.wait_for_timeout(2000)

        # 데이터분석 > 마케팅분석 페이지로 직접 이동
        page.goto(
            'https://sell.smartstore.naver.com/#/naverpay/analytics/marketing',
            wait_until='domcontentloaded', timeout=20000
        )
        page.wait_for_timeout(4000)

        # 검색채널 탭 클릭 시도
        for selector in ['text=검색채널', '[class*="tab"] >> text=검색채널', 'a:has-text("검색채널")']:
            try:
                el = page.query_selector(selector)
                if el:
                    el.click()
                    page.wait_for_timeout(2500)
                    print('  [키워드] 검색채널 탭 클릭 완료')
                    break
            except Exception:
                pass

        # 테이블 로딩 대기
        try:
            page.wait_for_selector('table tbody tr', timeout=10000)
        except Exception:
            print('  [키워드] 테이블 로딩 대기 시간 초과')

        page.wait_for_timeout(2000)

        # 테이블 데이터 추출
        raw = page.evaluate('''() => {
            const rows = Array.from(document.querySelectorAll('table tbody tr'));
            return rows.map(row => {
                const cells = Array.from(row.querySelectorAll('td'));
                return cells.map(c => c.innerText.trim());
            }).filter(r => r.length >= 2 && r[0] && r[0] !== '데이터가 없습니다');
        }''')

        def to_int(s):
            if not s:
                return None
            s = str(s).replace(',', '').replace(' ', '').strip()
            try:
                return int(float(s))
            except Exception:
                return None

        def to_float(s):
            if not s:
                return None
            s = str(s).replace('%', '').replace(',', '.').strip()
            try:
                return float(s)
            except Exception:
                return None

        for row in raw:
            keyword = row[0].strip()
            if not keyword or keyword in ('검색어', '합계', '전체'):
                continue
            results.append({
                'keyword': keyword,
                'visits': to_int(row[1]) if len(row) > 1 else None,
                'purchases': to_int(row[2]) if len(row) > 2 else None,
                'conversion_rate': to_float(row[3]) if len(row) > 3 else None,
                'revenue': to_int(row[4]) if len(row) > 4 else None,
            })

        print(f'  [키워드] {len(results)}개 키워드 수집 완료')

    except Exception as e:
        print(f'  [키워드] 수집 오류: {str(e)[:100]}')

    return results


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
                # 네이버 쇼핑 검색으로 구매수/찜수/등록일/유기순위 수집 (메모=스토어명으로 카드 매칭)
                memo = prod.get('memo', '') or ''
                if memo.strip():
                    print(f'    쇼핑 검색 중 (힌트: {memo[:20]})...')
                    shopping = search_naver_shopping(prod['name'], memo, page)
                    for k, v in shopping.items():
                        if v is None or k == 'error':
                            continue
                        if k in ('review_count', 'rating', 'price'):
                            # URL에서 이미 가져온 경우 유지, 실패한 경우만 보완
                            if r.get(k) is None:
                                r[k] = v
                        else:
                            # 쇼핑 전용 필드(구매수/찜수/등록일/유기순위)는 쇼핑 값 우선
                            r[k] = v
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

    # 키워드 수집 (스마트스토어 마케팅분석) — 판매자 계정 별도 세션 사용
    print('\n[키워드 수집 시작]')
    STORE_SESSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'smartstore_session')
    os.makedirs(STORE_SESSION_DIR, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=STORE_SESSION_DIR,
            headless=False,
            no_viewport=True,
            args=['--disable-blink-features=AutomationControlled'],
        )
        kw_page = context.new_page()
        keywords = scrape_smartstore_keywords(kw_page)
        context.close()

    if keywords:
        try:
            resp = requests.post(
                f'{SERVER_URL}/api/push_keywords',
                json=keywords,
                headers={'Content-Type': 'application/json'},
                timeout=15
            )
            data = resp.json()
            print(f'키워드 전송 완료: {data.get("saved", 0)}개 저장')
        except Exception as e:
            print(f'키워드 전송 실패: {e}')
    else:
        print('  키워드 데이터 없음 (스마트스토어 로그인 필요하거나 데이터 없음)')

    print(f'\n수집 완료! 브라우저에서 확인: {SERVER_URL}')


if __name__ == '__main__':
    main()
