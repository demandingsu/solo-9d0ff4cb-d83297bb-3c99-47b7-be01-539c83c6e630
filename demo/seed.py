"""Seed the demo site with a couple of notices on first start."""
from __future__ import annotations

from demo.store import DemoStore

SAMPLE_ATTACHMENT = """市政采购项目招标文件（示范）

项目名称：市政道路照明设备采购
项目编号：ZFCG-2026-0001
采购人：某市市政工程管理处

本文件为本地示范站生成的示例附件，仅用于验证档案采集与留存流程。
""".encode("utf-8")

SAMPLE_ATTACHMENT_2 = """市政采购项目询价文件（示范）

项目名称：城区绿化养护服务采购
项目编号：ZFCG-2026-0002
采购人：某市园林绿化中心

本文件为本地示范站生成的示例附件，仅用于验证档案采集与留存流程。
""".encode("utf-8")

NOTICES = [
    {
        "number": "ZFCG-2026-0001",
        "title": "市政道路照明设备采购招标公告",
        "body": (
            "一、项目概况\n"
            "采购市政道路照明设备一批，预算金额 480 万元。\n\n"
            "二、投标人资格\n"
            "具有独立法人资格及相应供货业绩。\n\n"
            "三、时间安排\n"
            "投标截止：2026 年 12 月 10 日 09:30（北京时间）。\n"
        ),
        "attachment_filename": "ZFCG-2026-0001-招标文件.txt",
        "attachment_content_type": "text/plain; charset=utf-8",
        "attachment_blob": SAMPLE_ATTACHMENT,
    },
    {
        "number": "ZFCG-2026-0002",
        "title": "城区绿化养护服务采购询价公告",
        "body": (
            "一、服务范围\n"
            "城区主干道绿化带日常养护，服务期一年。\n\n"
            "二、报价要求\n"
            "请于 2026 年 11 月 30 日前提交密封报价。\n"
        ),
        "attachment_filename": "ZFCG-2026-0002-询价文件.txt",
        "attachment_content_type": "text/plain; charset=utf-8",
        "attachment_blob": SAMPLE_ATTACHMENT_2,
    },
]


def ensure_seed(store: DemoStore) -> None:
    if store.list():
        return
    for item in NOTICES:
        if not store.exists(item["number"]):
            store.create(**item)
