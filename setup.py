#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Setup file for Linux distribution
Usage:  python setup.py sdist   --> to create a tarball
        python setup.py install --> to install in python directory
'''
# from distutils.core import setup

from setuptools import setup, find_packages

import orchestrator

setup(
    name='Orchestrator',
    version=orchestrator.__version__,
    packages=find_packages(),
    author='Thanassis Tsiodras',
    author_email='ttsiodras@gmail.com',
    description='Builder script for TASTE applications',
    long_description=open('README.md').read(),
    include_package_data=True,
    url='http://taste.tuxfamily.org',
    classifiers=[
        'Programming Language :: Python',
        'License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 2.7'
    ],
    entry_points={
        'console_scripts': [
            'taste-orchestrator = orchestrator.taste_orchestrator:main',
            'taste-patch-aplc = orchestrator.patchAPLCs:main',
            'taste-check-stack-usage = orchestrator.checkStackUsage:main'
        ]
    },
)
