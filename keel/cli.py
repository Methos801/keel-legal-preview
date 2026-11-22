"""Offline legal-workflow preview."""
import argparse
import json
from keel.skills.loader import SkillLoader
from keel.legal.controls import extract_chronology

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['skills', 'chronology'])
    action = parser.parse_args().action
    if action == 'skills':
        print('\n'.join(SkillLoader().get_prefix_index('legal')))
    else:
        rows = [{'sid':'S0001','locator':'p. 1','text':'Sample delivery on 2026-04-02.'}]
        print(json.dumps(extract_chronology(rows), indent=2))

if __name__ == '__main__': main()
