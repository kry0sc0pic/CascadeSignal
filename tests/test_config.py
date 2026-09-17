from cascadesignal.config import load_config


def test_load_config -> None:
 config = load_config

 assert config.name == "CascadeSignal"
 assert config.data.root.as_posix == "data"
