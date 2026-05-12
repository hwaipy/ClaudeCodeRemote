# Sphinx Documentation Guide

This project uses Sphinx with the **Furo** theme.

- Main language: **English** (`en`)
- Secondary language: **Chinese** (`zh_CN`)

## Contents (per language)

1. Introduction  
2. Quickstart (install + first commands)  
3. CLI reference (including online processing options)  
4. Offline processing (load ``.datablock`` and replay analysers)  
5. Extending (plugins, factories, analysers)  
6. DataBlock storage format (binary layout + size notes)  
7. API reference (AutoAPI)

## Install doc dependencies

```bash
python -m pip install -r requirements-docs.txt
```

## Build docs

From repository root:

```bash
make -C docs html-en
make -C docs html-zh
```

Outputs:

- English site: `docs/build/html/en/`
- Chinese site: `docs/build/html/zh_CN/`

You can also run the default Sphinx target:

```bash
make -C docs html
```
