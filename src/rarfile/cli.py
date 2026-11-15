"""Minimal command-line interface for rarfile module.
"""

import argparse

from .archive import RarFile

__all__ = ("main",)


def main(args: list[str]) -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                prog="python3 -m rarfile")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("-l", "--list", metavar="<rarfile>",
                   help="Show archive listing")
    g.add_argument("-e", "--extract", nargs=2,
                   metavar=("<rarfile>", "<output_dir>"),
                   help="Extract archive into target dir")
    g.add_argument("-t", "--test", metavar="<rarfile>",
                   help="Test if a archive is valid")
    g.add_argument("-v", "--verify", metavar="<rarfile>",
                   help="Verify archive, show per-file evidence levels")
    cmd = p.parse_args(args)

    if cmd.list:
        with RarFile(cmd.list) as rf:
            rf.printdir()
    elif cmd.test:
        with RarFile(cmd.test) as rf:
            rf.testrar()
    elif cmd.verify:
        with RarFile(cmd.verify) as rf:
            rep = rf.verify()
        for res in rep:
            print(res)
        if rep.parse_error:
            print("archive parse problem: %s" % rep.parse_error)
        if not rep.ok:
            p.exit(1)
    elif cmd.extract:
        with RarFile(cmd.extract[0]) as rf:
            rf.extractall(cmd.extract[1])
