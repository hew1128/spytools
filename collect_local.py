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
    page.wait_for_timeout(1000)
    page.fill('#id', naver_id)
    page.wait_for_timeout(500)
    page.fill('#pw', naver_pw)
    page.wait_for_timeout(500)
    page.click('.btn_login')
    page.wait_for_timeout(3000)
    # 로그인 성공 확인
    if 'naver.com' in page.url and 'nidlogin' not in page.url:
        print('  로그인 성공!')
        return True
    print('  로그인 실패 또는 2차 인증 필요. 직접 로그인 후 Enter 누르세요...')
    input()
    return True


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

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36', locale='ko-KR')

        # 네이버 상품이 있으면 로그인
        has_naver = any(p['platform'] == 'naver' for p in products)
        if has_naver and naver_id:
            naver_login(page, naver_id, naver_pw)

        for prod in products:
            pid      = prod['id']
            name     = prod['name']
            url      = prod['url']
            platform = prod['platform']
            print(f'  수집: {name[:35]}')

            if platform == 'naver':
                r = scrape_naver(url, page)
            elif platform == 'coupang':
                r = scrape_coupang(url, page)
            else:
                r = {'review_count': None, 'rating': None, 'price': None, 'error': f'지원안함:{platform}'}

            r['product_id'] = pid
            results.append(r)
            status = f"리뷰:{r['review_count']} 가격:{r['price']}" if r['review_count'] is not None else f"오류:{r.get('error','?')}"
            print(f'    → {status}')
            time.sleep(1)

        browser.close()

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
