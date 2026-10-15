This folder contains the official Python 3.13.2 "embeddable package" for Windows x64,
published by the Python Software Foundation (https://www.python.org/).
It lets the tool run without installing anything. Every .exe/.dll/.pyd file here is
digitally signed by the Python Software Foundation (vcruntime*.dll by Microsoft).
License: see LICENSE.txt. Removed from the original package (not needed by this tool):
sqlite3.dll, _sqlite3.pyd, winsound.pyd. python313._pth adds ..\..\engine to the search path.

本文件夹是 Python 软件基金会发布的官方 Windows 64 位“嵌入式 Python 3.13.2”，
让工具无需安装即可运行。删除本文件夹后，工具会改用电脑上已安装的 Python。
