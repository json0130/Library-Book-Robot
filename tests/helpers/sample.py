"""The sample book graph built from the real data/ files, built once and shared. Tests must not change it."""

from functools import lru_cache

from robot.config import load_config
from robot.services.book_graph import BookGraph


@lru_cache(maxsize=None)
def sample_graph() -> BookGraph:
    cfg = load_config()
    books = cfg.books
    return BookGraph.from_files(cfg.path(books["catalog_path"]), cfg.path(books["topics_path"]),
                                cfg.path(books["shelves_path"]), cfg.path(books["links_path"]),
                                weights=books["similarity"])
