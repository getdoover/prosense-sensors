from pydoover.docker import run_app

from .application import ProSenseApplication


def main():
    """Run the ProSense sensors application."""
    run_app(ProSenseApplication())
