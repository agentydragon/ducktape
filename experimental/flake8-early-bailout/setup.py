from setuptools import setup

setup(
    name="flake8-early-bailout",
    version="0.2.0",
    py_modules=["flake8_early_bailout"],
    install_requires=["flake8>=3.0.0"],
    entry_points={"flake8.extension": ["EB = flake8_early_bailout:EarlyBailoutChecker"]},
)
