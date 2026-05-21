# tune-plugin-example

Template de plugin pour Tune Server. Renommer et personnaliser.

Plugin template for Tune Server. Rename and customize.

## Installation

```bash
pip install -e .
tune-server  # plugin auto-discovered at startup
```

## Configuration

Variables d'environnement / Environment variables:

```bash
TUNE_EXAMPLE_ENABLED=true
TUNE_EXAMPLE_GREETING="Hello!"
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/example/hello` | Health check |
| GET | `/api/v1/example/echo/{message}` | Echo back a message |

## Tests

```bash
pip install -e ".[test]"
pytest tests/
```

## Personaliser / Customize

1. Renommer `my_tune_plugin/` et mettre a jour `pyproject.toml`
2. Editer `plugin.py` avec votre logique
3. Ajouter vos routes dans `routes.py`
4. Mettre a jour les entry points dans `pyproject.toml`

## Documentation

- [Plugin Developer Guide](https://github.com/renesenses/tune-server-linux/blob/main/docs/plugins/README.md)
- [Cookbook](https://github.com/renesenses/tune-server-linux/blob/main/docs/plugins/COOKBOOK.md)
