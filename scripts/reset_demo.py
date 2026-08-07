"""Reset demo by re-running seed_datahub script to restore baseline graph."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seed_datahub import main as seed_main


def main() -> None:
    print("Resetting DataHub ML graph to baseline...")
    seed_main()


if __name__ == "__main__":
    main()
