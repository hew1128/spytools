"""
염탐기 로컬 수집 스크립트
- 이 PC의 Chrome 브라우저로 네이버/쿠팡에 직접 접속해서 데이터 수집
- 수집 결과를 Railway 서버에 전송
- 실행: python collect_local.py
- 자동 실행: 윈도우 작업 스케줄러로 매일 아침 등록 가능
"""

import re
import json
import time
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ── 설정 ──────────────────────────────────────────────────────────
SERVER_URL = 'https://web-production-54ce2d.up.railway.app'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept-Language': 'ko-KR,ko;q=0.9',
}


def scrape_naver(url, page):
    try:
        page.goto(url, wait_until='networkidle', timeout=30000)
        page.wait_for_timeout(2000)
        content = page.content()
        soup = BeautifulSoup(content, 'html.parser')
        result = {'review_count': None, 'rating': None, 'price': None, 'error': None}

        next_script = soup.find('script', {'id': '__NEXT_DATA__'})
        if not next_script or not next_script.string:
            title = soup.find('title')
            result['error'] = f'NO_NEXT_DATA (title={title.text[:40] if title else "없음"})'
            return result

        s = next_script.string
        m = re.search(r'"reviewCount"\s*:\s*(\d+)', s)
        if m: result['review_count'] = int(m.group(1))
        m = re.search(r'"averageRating"\s*:\s*([\d.]+)', s)
        if m: result['rating'] = float(m.group(1))
        for pat in [r'"salePrice"\s*:\s*(\d{3,7})', r'"discountedSalePrice"\s*:\s*(\d{3,7})']:
            m = re.search(pat, s)
            if m:
                result['price'] = int(m.group(1))
                break
        if result['review_count'] is None:
            for pat in [r'"totalReviewCount"\s*:\s*(\d+)', r'"reviewTotalCount"\s*:\s*(\d+)']:
                m = re.search(pat, s)
                if m:
                    result['review_count'] = int(m.group(1))
                    break
        return result
    except Exception as e:
        return {'review_count': None, 'rating': None, 'price': None, 'error': str(e)[:200]}


def scrape_coupang(url, page):
    try:
        page.goto(url, wait_until='networkidle', timeout=30000)
        page.wait_for_timeout(2000)
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
    print(f'[염탐기 로컬 수집기]')
    print(f'서버: {SERVER_URL}')

    # 1. 상품 목록 가져오기
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

    # 2. Playwright로 수집
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # headless=False: 브라우저 창 보임
        page = browser.new_page(
            user_agent=HEADERS['User-Agent'],
            locale='ko-KR',
        )
        for prod in products:
            pid      = prod['id']
            name     = prod['name']
            url      = prod['url']
            platform = prod['platform']
            print(f'  수집 중: {name[:30]}...')
            if platform == 'naver':
                r = scrape_naver(url, page)
            elif platform == 'coupang':
                r = scrape_coupang(url, page)
            else:
                r = {'review_count': None, 'rating': None, 'price': None, 'error': f'지원안함:{platform}'}

            r['product_id'] = pid
            results.append(r)
            status = f"리뷰:{r['review_count']} 가격:{r['price']}" if r['review_count'] else f"오류:{r.get('error','?')}"
            print(f'    → {status}')
            time.sleep(1)

        browser.close()

    # 3. 서버로 전송
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

    print('\n수집 완료! 브라우저에서 확인하세요.')
    print(SERVER_URL)


if __name__ == '__main__':
    main()
