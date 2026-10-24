"""Termux CLI. Cleanup is read-only unless --apply is supplied."""

from __future__ import annotations

import argparse
import math
import sys
import time

from . import __version__
from .cleaner import DEFAULT_DAYS, DEFAULT_MAX_MIB, cache_roots, clean, scan
from .device import collect_status, launch_game, wake_lock


def size(value: int | None) -> str:
    if value is None:
        return "อ่านไม่ได้"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return str(value)


def _number(minimum: float, maximum: float | None = None):
    def parse(value: str) -> float:
        try:
            number = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError("ต้องเป็นตัวเลข") from None
        if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
            upper = f" ถึง {maximum:g}" if maximum is not None else " ขึ้นไป"
            raise argparse.ArgumentTypeError(f"ต้องอยู่ในช่วง {minimum:g}{upper}")
        return number
    return parse


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="ดูแลแคช Termux แบบไม่รูท — ไม่แตะเซฟเกม/รูปภาพ และไม่รับประกัน FPS",
    )
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command")
    commands.add_parser("menu", help="เมนูภาษาไทย (ค่าเริ่มต้น)")
    commands.add_parser("status", help="ดู RAM พื้นที่ว่าง แบตเตอรี่ และอุณหภูมิที่อ่านได้")
    for name, help_text in (
        ("scan", "ดูไฟล์แคชเก่าที่เข้าเกณฑ์ โดยไม่ลบ"),
        ("clean", "แสดงตัวอย่าง; เพิ่ม --apply เพื่อลบจริง"),
        ("auto", "ตรวจแคชเป็นรอบใน foreground; Ctrl+C เพื่อหยุด"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--days", type=_number(1), default=DEFAULT_DAYS, help="อายุขั้นต่ำเป็นวัน (ค่าเริ่มต้น 7, อย่างน้อย 1)")
        command.add_argument("--max-mib", type=_number(0.001, 4096), default=DEFAULT_MAX_MIB, help="เพดานขนาดไฟล์ต่อรอบ MiB (ค่าเริ่มต้น 256)")
        if name != "scan":
            command.add_argument("--apply", action="store_true", help="ยืนยันลบไฟล์แคชที่เข้าเกณฑ์จริง")
        if name == "auto":
            command.add_argument("--interval", type=_number(60), default=3600.0, help="วินาทีระหว่างรอบ (ค่าเริ่มต้น 3600, อย่างน้อย 60)")
    game = commands.add_parser("game", help="เปิดเกมที่ติดตั้งแล้ว โดยไม่ปรับ CPU/GPU หรือฆ่าแอป")
    game.add_argument("package", help="Android package เช่น com.example.game")
    game.add_argument("--wake-lock", action="store_true", help="ถือ partial wake lock จนหยุดคำสั่ง (อาจกินแบต)")
    game.add_argument("--watch", action="store_true", help="ดูสถานะต่อเนื่องจนกด Ctrl+C")
    game.add_argument("--refresh", type=_number(2), default=5.0, help="ช่วงดูสถานะเป็นวินาที (ค่าเริ่มต้น 5)")
    return result


def show_status() -> None:
    status = collect_status()
    memory = status["memory"]
    disk = status["disk"]
    print(f"RAM ใช้ได้: {size(memory['available_bytes'])} / {size(memory['total_bytes'])}")
    print(f"พื้นที่ว่าง (ที่เก็บ Termux): {size(disk['free_bytes'])} / {size(disk['total_bytes'])}")
    battery = status.get("battery")
    if battery:
        print(f"แบตเตอรี่: {battery.get('percentage', 'อ่านไม่ได้')}% | อุณหภูมิแบต: {battery.get('temperature', 'อ่านไม่ได้')} °C")
    else:
        print("แบตเตอรี่: อ่านไม่ได้ (Termux:API เป็นตัวเลือกเสริม)")
    thermals = status.get("thermal", [])
    if thermals:
        for sensor in thermals[:12]:
            print(f"เซนเซอร์ {sensor['name']}: {sensor['celsius']:.1f} °C")
    else:
        print("อุณหภูมิระบบ: Android ไม่เปิดให้อ่าน/ไม่พบเซนเซอร์")


def run_cleanup(args: argparse.Namespace) -> int:
    apply = getattr(args, "apply", False)
    print("โหมด: " + ("ลบแคชจริง" if apply else "ดูตัวอย่างเท่านั้น — ยังไม่ลบไฟล์"))
    roots = cache_roots()
    for root in roots:
        print(f"แคช: {str(root.path)!r}")
    snapshot = scan(roots, days=args.days, max_mib=args.max_mib)
    print(f"ตรวจ {snapshot.examined_files} รายการ | เข้าเกณฑ์ {snapshot.eligible_files} ไฟล์ ({size(snapshot.eligible_bytes)})")
    print(f"ภายในเพดาน {args.max_mib:g} MiB: {len(snapshot.candidates)} ไฟล์ ({size(snapshot.selected_bytes)})")
    for item in snapshot.candidates[:15]:
        # repr prevents control characters in filenames from controlling the terminal.
        print(f"  {size(item.size):>12}  {str(item.path)!r}")
    if len(snapshot.candidates) > 15:
        print(f"  ... อีก {len(snapshot.candidates) - 15} ไฟล์")
    for warning in snapshot.warnings:
        print(f"ข้าม/ข้อจำกัด: {warning!r}", file=sys.stderr)
    if not apply:
        if args.command == "clean":
            print("ถ้าต้องการลบจริง ใช้คำสั่งเดิมพร้อม --apply")
        return 1 if snapshot.warnings else 0
    result = clean(snapshot)
    print(f"ลบแล้ว {result.removed_files} ไฟล์ ขนาดรวม {size(result.removed_bytes)} | ข้าม {result.skipped_files} ไฟล์ที่เปลี่ยนแปลง/เข้าไม่ได้")
    print("พื้นที่คืนจริงอาจต่างจากขนาดไฟล์ และแอปอาจสร้างแคชใหม่")
    for warning in result.warnings:
        print(f"ข้าม/ข้อจำกัด: {warning!r}", file=sys.stderr)
    return 1 if snapshot.warnings or result.skipped_files else 0


def run_auto(args: argparse.Namespace) -> int:
    print(f"ทำงานทุก {args.interval:g} วินาที; Ctrl+C เพื่อหยุด ไม่ติดตั้งบริการเบื้องหลัง")
    print("Android อาจหยุด Termux เมื่ออยู่เบื้องหลัง; ใช้ auto อย่างเดียวโดยไม่ต้องเปิดเกมค้างไว้")
    while True:
        print(time.strftime("\n[%Y-%m-%d %H:%M:%S]"))
        result = run_cleanup(args)
        if result:
            print("รอบนี้มีรายการข้าม ดูข้อความด้านบน; รอบถัดไปจะสแกนใหม่", file=sys.stderr)
        time.sleep(args.interval)


def run_game(args: argparse.Namespace) -> int:
    if args.wake_lock:
        print("wake lock ใช้ร่วมกันทั้ง Termux; อย่าใช้ตัวเลือกนี้ถ้างานอื่นถือ lock อยู่")
    with wake_lock(args.wake_lock):
        print(launch_game(args.package))
        print("เปิดเกมแล้ว; ไม่ได้ปรับ CPU/GPU หรือเพิ่ม FPS")
        if args.wake_lock:
            print("ถือ wake lock จนกด Ctrl+C; อาจกินแบตและเพิ่มความร้อน")
        while True:
            show_status()
            if not args.watch and not args.wake_lock:
                return 0
            if args.wake_lock and not args.watch:
                # Keep ownership of the lock until explicitly interrupted.
                time.sleep(60)
            else:
                time.sleep(args.refresh)


def menu() -> int:
    if not sys.stdin.isatty():
        parser().print_help()
        return 0
    while True:
        print("\nXiaomi Speed Boost — Termux ไม่รูท")
        print("1. ดูสถานะเครื่อง\n2. สแกนแคช\n3. ล้างแคช (ดูรายการก่อนยืนยัน)\n4. ดูวิธีตั้งเวลา\n5. เปิดเกม\n0. ออก")
        choice = input("เลือก: ").strip()
        if choice == "0":
            return 0
        if choice == "1":
            show_status()
        elif choice in {"2", "3"}:
            args = parser().parse_args(["scan"])
            if choice == "2":
                run_cleanup(args)
            else:
                # Delete precisely this preview, never a second unreviewed scan.
                snapshot = scan()
                print(f"แคชเก่ากว่า 7 วัน สูงสุด 256 MiB: {len(snapshot.candidates)} ไฟล์ ({size(snapshot.selected_bytes)})")
                for item in snapshot.candidates[:15]:
                    print(f"  {str(item.path)!r}")
                for warning in snapshot.warnings:
                    print(f"ข้าม/ข้อจำกัด: {warning!r}", file=sys.stderr)
                if snapshot.candidates and input("พิมพ์ DELETE เพื่อยืนยันลบรายการแคชนี้: ").strip() == "DELETE":
                    result = clean(snapshot)
                    print(f"ลบ {result.removed_files} ไฟล์ ({size(result.removed_bytes)}), ข้าม {result.skipped_files}")
                    for warning in result.warnings:
                        print(f"ข้าม/ข้อจำกัด: {warning!r}", file=sys.stderr)
        elif choice == "4":
            print("ทดลอง: python boost.py auto --interval 3600")
            print("ลบจริง: python boost.py auto --interval 3600 --apply")
            print("รันใน foreground, Ctrl+C เพื่อหยุด; ลบเฉพาะแคชที่ไม่ได้ใช้/แก้ไขเกิน 7 วัน")
        elif choice == "5":
            package = input("ชื่อ package ของเกม: ").strip()
            try:
                print(launch_game(package))
            except (RuntimeError, ValueError) as error:
                print(f"เปิดเกมไม่ได้: {error}", file=sys.stderr)
        else:
            print("เลือกหมายเลข 0–5")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {None, "menu"}:
            return menu()
        if args.command == "status":
            show_status()
            return 0
        if args.command in {"scan", "clean"}:
            return run_cleanup(args)
        if args.command == "auto":
            return run_auto(args)
        if args.command == "game":
            return run_game(args)
    except KeyboardInterrupt:
        print("\nหยุดแล้ว (ถ้ามี wake lock ที่คำสั่งนี้ถืออยู่ จะคืนให้)")
        return 130
    except EOFError:
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"ทำงานไม่ได้: {error}", file=sys.stderr)
        return 1
    return 0
