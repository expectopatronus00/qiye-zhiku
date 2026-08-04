# -*- coding: utf-8 -*-
"""生成 Day5 测试 PDF: 含标题层级、正文、表格、内嵌图片(带文字)"""
import os
import io
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_docs", "layout_test.pdf")


def _make_chart_image() -> bytes:
    """生成一张带文字的图片(模拟图表/截图)"""
    img = Image.new("RGB", (600, 300), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 580, 280], outline=(0, 0, 0), width=2)
    # 模拟柱状图
    bars = [(80, 90), (180, 130), (280, 75), (380, 160), (480, 110)]
    for i, (x, h) in enumerate(bars):
        d.rectangle([x, 260 - h, x + 60, 260], fill=(70, 130, 180))
    d.text((150, 20), "GPU Utilization Monitor", fill=(0, 0, 0))
    d.text((150, 45), "RTX 4070 SUPER: 85%", fill=(200, 0, 0))
    d.text((150, 70), "功耗: 220W", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main():
    import fitz  # PyMuPDF

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4

    y = 60
    # 标题(h1)
    page.insert_text((60, y), "服务器硬件监控方案", fontsize=20, fontname="china-s")
    y += 50
    # 标题(h2)
    page.insert_text((60, y), "一、监控指标概览", fontsize=15, fontname="china-s")
    y += 40
    body = "本方案对数据中心服务器进行7x24小时监控，重点关注CPU温度、GPU利用率与内存占用。"
    page.insert_text((60, y), body, fontsize=10, fontname="china-s")
    y += 35
    page.insert_text((60, y), "当GPU利用率超过90%时触发告警，并通过企业微信通知值班人员。",
                     fontsize=10, fontname="china-s")
    y += 50

    # 表格: 手绘线框 + 文本
    page.insert_text((60, y), "二、监控阈值表", fontsize=15, fontname="china-s")
    y += 30
    col_x = [60, 200, 340, 480]
    rows_data = [
        ["指标", "正常区间", "告警阈值", "级别"],
        ["CPU温度", "40-70C", ">85C", "严重"],
        ["GPU利用率", "30-80%", ">90%", "警告"],
        ["内存占用", "<70%", ">85%", "警告"],
        ["磁盘IO", "<60%", ">80%", "提示"],
    ]
    row_h = 24
    # 画横线
    for i in range(len(rows_data) + 1):
        page.draw_line((col_x[0], y + i * row_h), (col_x[-1] + 100, y + i * row_h), width=1)
    # 画竖线
    for cx in col_x:
        page.draw_line((cx, y), (cx, y + len(rows_data) * row_h), width=1)
    page.draw_line((col_x[-1] + 100, y), (col_x[-1] + 100, y + len(rows_data) * row_h), width=1)
    # 填文字
    for r_i, row in enumerate(rows_data):
        for c_i, cell in enumerate(row):
            page.insert_text((col_x[c_i] + 6, y + r_i * row_h + 16), cell,
                             fontsize=10, fontname="china-s")
    y += len(rows_data) * row_h + 40

    # 图片(含文字, 走 OCR)
    page.insert_text((60, y), "三、GPU 监控截图", fontsize=15, fontname="china-s")
    y += 20
    page.insert_image(fitz.Rect(60, y, 460, y + 200), stream=_make_chart_image())
    y += 220

    page.insert_text((60, y), "四、告警通知流程", fontsize=15, fontname="china-s")
    y += 35
    page.insert_text((60, y), "告警触发后由监控平台调用企业微信机器人接口推送消息，"
                     "10分钟内未确认则升级为短信通知。", fontsize=10, fontname="china-s")

    doc.save(OUT)
    doc.close()
    print("written:", OUT)


if __name__ == "__main__":
    main()
