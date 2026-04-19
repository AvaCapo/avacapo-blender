# AvaCapo blender addon.
------------------------

Bledner integration of AvaCapo.
Addon allows you to use AI for animating characters.

This repo root is blender addon folder, it have __init__.py

### Installation:
Dowload zip and install it via blender:
Get Extentions -> Install from disk

### Development setup:
Use Vscode Blender Development extention in this folder, it will symlink the folder
in local blender addons folder.

#### or just do symlink:
ln -s ~/gits/avacapo-blender ~/.config/blender/5.0/extensions/user_default/avacapo

and for restarting just reload manually in settings
folder name should be the same as name in manifest

### formatting:
pep8 + linelength 100 (according to blender docs)
black used as formatter
