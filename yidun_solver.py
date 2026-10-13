#!/usr/bin/env python3
"""
网易易盾 (NetEase Yidun) 滑块验证码求解器

使用方法:
  pip install cloakbrowser opencv-python-headless numpy
  python yidun_solver.py

环境要求:
  - Python 3.8+
  - CloakBrowser (隐身 Chromium，自动绕过 bot 检测)
  - OpenCV + NumPy (缺口定位)
  - 网络能访问 dun.163.com

原理:
  1. 用 OpenCV 模板匹配定位缺口 X 坐标
  2. 拖拽距离 = gap_x + 10.5px (滑块中心与 jigsaw 左边缘的固定偏移)
  3. 模拟人类轨迹拖拽 (三段式 easing + 微回退 + 过冲校正)
  4. 失败自动重试 (服务端有随机风控，单次约 60-70%，重试后达 80-90%)
"""

import sys, time, random, urllib.request, re

try:
    import numpy as np
    import cv2
except ImportError:
    print("需要安装: pip install opencv-python-headless numpy")
    sys.exit(1)

try:
    from cloakbrowser import launch
except ImportError:
    print("需要安装: pip install cloakbrowser")
    print("CloakBrowser 是隐身 Chromium，自动绕过 bot 检测")
    sys.exit(1)

# ============================================================
# 配置
# ============================================================
OFFSET = 10.5          # 滑块中心 → jigsaw 左边缘偏移 (实测值)
N_ROUNDS = 10          # 求解轮数
MAX_RETRIES = 4        # 每轮最大重试次数
HEADLESS = True         # True=无头模式, False=显示浏览器窗口

# ============================================================
# 缺口检测 — 用拼图块做模板匹配
# ============================================================
def detect_gap(bg_bgr, piece_bgra):
    """
    用模板匹配在背景图中定位拼图块缺口位置。

    参数:
        bg_bgr:    背景图 (h, w, 3)，OpenCV BGR 格式，不要转置！
        piece_bgra: 拼图块 (h, w, 4)，BGRA 格式

    返回:
        (gap_x, score): 缺口 X 坐标 + 匹配置信度
    """
    gray = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY)
    piece_rgb = piece_bgra[:, :, :3]
    piece_gray = cv2.cvtColor(piece_rgb, cv2.COLOR_BGR2GRAY)
    mask = None
    if piece_bgra.shape[2] == 4:
        mask = (piece_bgra[:, :, 3] > 20).astype(np.uint8) * 255
    try:
        res = cv2.matchTemplate(gray, piece_gray, cv2.TM_CCORR_NORMED, mask=mask)
    except Exception:
        res = cv2.matchTemplate(gray, piece_gray, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return int(max_loc[0]), float(max_val)


# ============================================================
# 人类化拖拽
# ============================================================
def human_drag(page, sx, sy, drag):
    """
    模拟人类拖拽滑块。

    三段式 easing: 慢启动 → 匀速中段 → 减速末端
    加入微回退、Y轴抖动、过冲校正等人类特征。
    """
    # 起始微移动 (模拟手放上去)
    page.mouse.move(sx + random.uniform(-1, 1), sy + random.uniform(-1, 1))
    time.sleep(random.uniform(0.3, 0.6))
    page.mouse.move(sx, sy)
    time.sleep(random.uniform(0.1, 0.2))
    page.mouse.down()
    time.sleep(random.uniform(0.30, 0.55))

    steps = random.randint(42, 60)
    # 起始微抖
    page.mouse.move(sx + random.uniform(0.5, 2.0), sy + random.uniform(-0.5, 0.5))
    time.sleep(random.uniform(0.02, 0.05))

    for j in range(1, steps + 1):
        t = j / steps
        # 三段式 easing
        if t < 0.35:
            ease = 0.5 * ((t / 0.35) ** 2.3)
        elif t < 0.7:
            ease = 0.5 + 0.4 * ((t - 0.35) / 0.35)
        else:
            dt = min((t - 0.7) / 0.3, 1.0)
            ease = 0.9 + 0.1 * (1.0 - (1.0 - dt) ** 1.5)
        ease = max(0.0, min(1.0, ease + random.uniform(-0.005, 0.005)))

        x = sx + drag * ease + random.uniform(-0.6, 0.6)
        y = sy + random.gauss(0, 0.8)
        page.mouse.move(x, y)

        # 随机微回退 (模拟人类手抖)
        if random.random() < 0.05 and 0.15 < t < 0.80:
            back = random.uniform(1.0, 3.0)
            page.mouse.move(x - back, y + random.uniform(-0.4, 0.4))
            time.sleep(random.uniform(0.02, 0.06))
            page.mouse.move(x + random.uniform(0, 1), y + random.uniform(-0.3, 0.3))

        # 变速
        r = random.random()
        if t < 0.08:
            time.sleep(random.uniform(0.05, 0.10))
        elif t < 0.20:
            time.sleep(random.uniform(0.030, 0.055))
        elif t < 0.75:
            if r < 0.7:
                time.sleep(random.uniform(0.010, 0.025))
            else:
                time.sleep(random.uniform(0.025, 0.045))
        elif t < 0.92:
            time.sleep(random.uniform(0.020, 0.050))
        else:
            time.sleep(random.uniform(0.040, 0.090))

    # 过冲 + 校正 (人类特征)
    over = random.uniform(2.0, 5.0)
    page.mouse.move(sx + drag + over, sy + random.uniform(-0.4, 0.4))
    time.sleep(random.uniform(0.08, 0.18))
    page.mouse.move(sx + drag + random.uniform(-0.3, 0.3), sy + random.uniform(-0.3, 0.3))
    time.sleep(random.uniform(0.25, 0.50))
    page.mouse.up()


# ============================================================
# 单次求解
# ============================================================
def solve_once(page):
    """
    执行一次验证码求解。

    返回: True=成功, False=失败
    """
    check = []
    def on_resp(resp):
        if 'api/v3/check' in resp.url:
            try: body = resp.text()
            except: body = 'ERR'
            check.append((resp.status, body[:500]))
    page.on('response', on_resp)

    page.goto('https://dun.163.com/trial/jigsaw', timeout=60000, wait_until='domcontentloaded')
    time.sleep(random.uniform(4.5, 6.0))
    try:
        page.locator('div.yidun_slider').first.hover()
        time.sleep(random.uniform(1.0, 1.6))
    except:
        pass

    # 提取页面信息
    info = page.evaluate("""() => {
      const s = document.querySelector('div.yidun_slider');
      const bg = document.querySelector('[class*=yidun_bg-img]');
      const jg = document.querySelector('[class*=yidun_jigsaw]');
      if (!s || !bg || !jg || !bg.src || !jg.src) return null;
      const sr = s.getBoundingClientRect();
      const br = bg.getBoundingClientRect();
      return {
        sx: sr.x + sr.width/2, sy: sr.y + sr.height/2,
        bgW: br.width, bgNatW: bg.naturalWidth,
        bgSrc: bg.src, jgSrc: jg.src
      };
    }""")
    if not info:
        print("no-captcha", end=" ", flush=True)
        page.remove_listener('response', on_resp)
        return False

    # 下载图片
    bg_url = info['bgSrc']
    piece_url = info['jgSrc']
    if bg_url.startswith('//'): bg_url = 'https:' + bg_url
    if piece_url.startswith('//'): piece_url = 'https:' + piece_url
    urllib.request.urlretrieve(bg_url, '/tmp/yidun_bg.jpg')
    urllib.request.urlretrieve(piece_url, '/tmp/yidun_piece.png')

    # 读取图片 — 注意：不要 transpose！
    bg = cv2.imread('/tmp/yidun_bg.jpg')
    piece = cv2.imread('/tmp/yidun_piece.png', cv2.IMREAD_UNCHANGED)
    if bg is None or piece is None:
        print("img-err", end=" ", flush=True)
        page.remove_listener('response', on_resp)
        return False

    # 检测缺口位置
    gap_x, score = detect_gap(bg, piece)
    disp_scale = info['bgW'] / info['bgNatW']
    drag = gap_x * disp_scale + OFFSET

    # 拖拽
    human_drag(page, info['sx'], info['sy'], drag)
    time.sleep(4.5)

    # 判断结果
    ok = False
    for status, body in check:
        m = re.search(r'"result"\s*:\s*(true|false)', body)
        if m and m.group(1) == 'true':
            ok = True
    result = page.evaluate("() => { const r=document.querySelector('.yidun'); return r?r.className:''; }")
    if 'success' in result:
        ok = True

    print('✅' if ok else '❌', end=' ', flush=True)
    page.remove_listener('response', on_resp)
    return ok


# ============================================================
# 主函数
# ============================================================
def main():
    browser = launch(headless=HEADLESS, humanize=False)
    page = browser.new_page()

    # 反检测
    page.add_init_script("""
      Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
      window.chrome = window.chrome || {runtime:{}};
    """)

    total_ok = 0
    total_attempts = 0

    for round_num in range(1, N_ROUNDS + 1):
        print(f"\nRound {round_num}:", end=" ")
        for retry in range(MAX_RETRIES):
            total_attempts += 1
            if solve_once(page):
                total_ok += 1
                break
            # 失败后冷却
            if retry < MAX_RETRIES - 1:
                time.sleep(random.uniform(1.0, 2.5))

    browser.close()
    pct = total_ok / N_ROUNDS * 100
    print(f"\n\n结果: {total_ok}/{N_ROUNDS} 轮成功 ({pct:.0f}%), 共 {total_attempts} 次尝试")


if __name__ == '__main__':
    main()