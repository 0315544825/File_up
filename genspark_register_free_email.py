#!/usr/bin/env python3
"""genspark 免费邮箱注册实测脚本 (mail.tm + B2C signup).

流程 (单会话, cookie 自动保持):
  1. mail.tm 建邮箱
  2. /api/login -> unified?local=signup
  3. SendCode(email) -> mail.tm 轮询 OTP
  4. VerifyCode(email, code)
  5. GetChallenge Audio -> 存 mp3 (人耳听写)
  6. VerifyChallenge(captcha答案)
  7. POST SelfAsserted 完成注册 -> confirmed 拿 code

用法:
  python3 genspark_register_free_email.py --out ./reg_out
  # 听完 mp3 后:
  python3 genspark_register_free_email.py --out ./reg_out --captcha 你听到的词 --email xxx@yyy --password Xx..(可选)

注意: captcha 必须人耳听写, 脚本不做 STT 自动解.
"""
from __future__ import annotations
import argparse, base64, json, random, re, string, sys, time, urllib.parse
from pathlib import Path

TENANT="gensparkad.onmicrosoft.com"; POLICY="B2C_1_new_login"
LOGIN_HOST="https://login.genspark.ai"; APP_HOST="https://www.genspark.ai"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"

def gen_password(n=14):
    sets=[string.ascii_lowercase,string.ascii_uppercase,string.digits,"!@#$%^&*"]
    pw=[random.choice(s) for s in sets]
    pw+=[random.choice("".join(sets)) for _ in range(n-len(pw))]
    random.shuffle(pw); return "".join(pw)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--out", default="reg_out")
    ap.add_argument("--captcha", default="", help="音频听写答案, 不填则只跑到存mp3")
    ap.add_argument("--email", default="", help="复用已有 mail.tm 邮箱 (需同目录 .mailtm.json)")
    ap.add_argument("--password", default="", help="指定密码, 默认随机")
    args=ap.parse_args()
    out=Path(args.out); out.mkdir(parents=True,exist_ok=True)

    sys.path.insert(0,"/root/.openclaw/workspace")
    from temp_mail_api import MailTM
    from curl_cffi import requests as cr
    s=cr.Session(impersonate="chrome124"); s.headers.update({"User-Agent":UA})

    # 1. 邮箱
    if args.email and (out/".mailtm.json").exists():
        cred=json.loads((out/".mailtm.json").read_text())
        c=MailTM(); c.address=cred["address"]; c.password=cred["password"]; c._get_token()
        email=c.address
        print(f"[1] 复用邮箱 {email}")
    else:
        c=MailTM(); email=c.create_account()
        (out/".mailtm.json").write_text(json.dumps({"address":c.address,"password":c.password}))
        print(f"[1] 新建 mail.tm 邮箱 {email}")

    password=args.password or gen_password()
    (out/".cred.txt").write_text(f"{email}\n{password}\n")
    print(f"    密码 {password[:2]}*** (存 {out/'.cred.txt'})")

    # 2. signup 页
    r=s.get(f"{APP_HOST}/api/login",params={"redirect_url":f"{APP_HOST}/"},allow_redirects=True,timeout=20)
    html=r.text
    csrf0=re.search(r'"csrf":"([^"]+)"',html).group(1); tx0=re.search(r'"transId":"([^"]+)"',html).group(1)
    tenant=re.search(r'"tenant":"([^"]+)"',html).group(1); policy=re.search(r'"policy":"([^"]+)"',html).group(1); api=re.search(r'"api":"([^"]+)"',html).group(1)
    signup_url=f"https://login.genspark.ai{tenant}/api/{api}/unified?local=signup&csrf_token={urllib.parse.quote(csrf0,safe='')}&tx={urllib.parse.quote(tx0,safe='')}&p={policy}"
    r2=s.get(signup_url,headers={"Referer":r.url},timeout=20)
    html2=r2.text
    csrf=re.search(r'"csrf":"([^"]+)"',html2).group(1); tx=re.search(r'"transId":"([^"]+)"',html2).group(1)
    print(f"[2] signup ok tx={tx[:40]}...")

    base=f"{LOGIN_HOST}/{TENANT}/{POLICY}/SelfAsserted/DisplayControlAction/vbeta"
    H={"X-CSRF-TOKEN":csrf,"Referer":signup_url,"Origin":LOGIN_HOST,"X-Requested-With":"XMLHttpRequest",
       "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"}

    # 3. SendCode + 轮询
    resp=s.post(f"{base}/emailVerificationControl/SendCode",params={"tx":tx,"p":POLICY},headers=H,data={"email":email},timeout=20)
    print(f"[3] SendCode {resp.status_code} {resp.text[:200]}")
    code=""
    for i in range(40):
        msgs=c.get_messages()
        if msgs:
            body=msgs[0].body or ""
            m=re.search(r"(\d{6})",body)
            if m: code=m.group(1); break
        time.sleep(3)
    if not code:
        print("OTP 未收到, 退出"); return 1
    print(f"[3] OTP={code}")

    # 4. VerifyCode
    resp=s.post(f"{base}/emailVerificationControl/VerifyCode",params={"tx":tx,"p":POLICY},headers=H,
                data={"email":email,"emailVerificationCode":code},timeout=20)
    print(f"[4] VerifyCode {resp.status_code} {resp.text[:500]}")

    # 5. GetChallenge Audio
    r3=s.get(f"{base}/captchaControlChallengeCode/GetChallenge",params={"tx":tx,"p":POLICY,"challengeType":"Audio"},
             headers={"X-CSRF-TOKEN":csrf,"Referer":signup_url,"Origin":LOGIN_HOST,"X-Requested-With":"XMLHttpRequest"},timeout=20)
    a=r3.json()
    cid=a["challengeId"]; az=a.get("azureregion") or ""
    _,b64=a["challengeString"].split(",",1)
    (out/"audio.mp3").write_bytes(base64.b64decode(b64))
    print(f"[5] Audio id={cid} az={az} 已存 {out/'audio.mp3'} 请播放听写")
    json.dump({"challengeId":cid,"azureregion":az,"csrf":csrf,"tx":tx,"signup_url":signup_url,"email":email,"code":code},
              open(out/"session.json","w"),indent=2)

    if not args.captcha:
        print("下一步: 听 mp3 后重跑 --captcha 听到的词 --email 复用")
        return 2

    # 6. VerifyChallenge
    resp=s.post(f"{base}/captchaControlChallengeCode/VerifyChallenge",params={"tx":tx,"p":POLICY},headers=H,
                data={"challengeId":cid,"captchaEntered":args.captcha,"challengeType":"Audio","azureRegion":az},timeout=20)
    print(f"[6] VerifyChallenge {resp.status_code} {resp.text[:500]}")
    try: vj=resp.json()
    except: print("Verify 非JSON, 停止"); return 1
    if not (str(vj.get("status"))=="200" and str(vj.get("isCaptchaSolved")).lower()=="true"):
        print("captcha 未通过, 请重听/换答案重试"); return 1

    # 7. 最终提交
    payload={"request_type":"RESPONSE","email":email,"emailVerificationCode":code,
             "newPassword":password,"reenterPassword":password,
             "captchaEntered":args.captcha,"challengeType":"Audio",
             "challengeId":cid,"challengeString":a["challengeString"]}
    resp=s.post(f"{LOGIN_HOST}/{TENANT}/{POLICY}/SelfAsserted",params={"tx":tx,"p":POLICY},headers=H,data=payload,timeout=20)
    print(f"[7] SelfAsserted {resp.status_code} {resp.text[:800]}")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
