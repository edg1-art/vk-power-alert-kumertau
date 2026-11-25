# -*- coding: utf-8 -*-
"""Локальные тесты без сети: фильтры, тексты меню, работа с state.json.

Запуск:  python selftest.py
Телеграм и ВК не нужны — проверяются только чистые функции.
"""

import json
import os
import sys
import tempfile

import vk_check as v

CFG = {
    "group_id": 11784072,
    "keywords_all": ["электроэнерг"],
    "keywords_any": ["тключен"],
    "streets": ["Авиаторов"],
    "street_mode": "mark",
    "community_url": "https://vk.com/toser_admkumertau",
}

POSTS = [
    {"id": 1, "date": "23 ноября 2026 в 9:00",
     "text": "Отключение электроэнергии: ул. Авиационная, дом 5, с 10:00 до 14:00."},
    {"id": 2, "date": "24 ноября 2026 в 7:36",
     "text": "Планируется отключение электроэнергии по ул. Авиаторов, дома 1-20, с 9:00 до 17:00."},
    {"id": 3, "date": "25 ноября 2026 в 12:00",
     "text": "Праздничный концерт ко Дню Матери. Приглашаем всех жителей."},
]


def check(name, cond):
    if not cond:
        print("ПРОВАЛ:", name)
        sys.exit(1)
    print("ok:", name)


def main():
    check("отключение по Авиаторов распознано", v.is_outage(POSTS[1]["text"], CFG))
    check("отключение по Авиационной распознано", v.is_outage(POSTS[0]["text"], CFG))
    check("концерт не считается отключением", not v.is_outage(POSTS[2]["text"], CFG))
    check("улица Авиаторов найдена", v.street_hits(POSTS[1]["text"], CFG) == ["Авиаторов"])
    check("Авиационная не путается с Авиаторов", v.street_hits(POSTS[0]["text"], CFG) == [])

    news = v.digest_news(CFG, POSTS)
    check("в новостях 3 записи", news.count("🔗") == 3)
    out = v.digest_outages(CFG, POSTS)
    check("в отключениях 2 записи", out.count("🔗") == 2)
    check("в отключениях помечена улица", "Авиаторов" in out)
    last = v.digest_last(CFG, POSTS)
    check("последнее отключение — по Авиаторов", "Авиаторов" in last and "wall-11784072_2" in last)
    status = v.digest_status(CFG, POSTS)
    check("статус показывает проверку", "каждые 5 минут" in status)

    prev, pool, new = v.compute_new(POSTS, {"max_id": 2})
    check("новые записи после 2", prev == 2 and pool == 3 and [p["id"] for p in new] == [3])
    prev, pool, new = v.compute_new(POSTS, {}, force_new_id=0)
    check("force_new_id=0 даёт все записи", [p["id"] for p in new] == [1, 2, 3])

    to_send = v.pick_to_send([POSTS[1], POSTS[2]], {}, CFG)
    check("на отправку попадает только отключение", [p["id"] for p, _ in to_send] == [2])

    only_cfg = dict(CFG, street_mode="only")
    to_send = v.pick_to_send(POSTS, {}, only_cfg)
    check("режим only оставляет только Авиаторов", [p["id"] for p, _ in to_send] == [2])

    off_cfg = dict(CFG, street_mode="off")
    to_send = v.pick_to_send(POSTS, {}, off_cfg)
    check("режим off не помечает улицу", "ВАША УЛИЦА" not in to_send[0][1])

    # state_patch не должен терять чужие ключи (tg_offset, menu_sent)
    old_path = v.STATE_PATH
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        v.STATE_PATH = tmp
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"max_id": 5, "tg_offset": 77, "sent": [1, 2]}, f)
        v.state_patch(sent=[3])
        with open(tmp, "r", encoding="utf-8") as f:
            data = json.load(f)
        check("state_patch сохранил max_id", data["max_id"] == 5)
        check("state_patch сохранил tg_offset", data["tg_offset"] == 77)
        check("state_patch обновил sent", data["sent"] == [3])
    finally:
        v.STATE_PATH = old_path
        os.remove(tmp)

    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
