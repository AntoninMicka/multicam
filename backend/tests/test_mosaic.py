import pytest

from app.mosaic import MosaicError, grid_size, mosaic_filter


def test_grid_size_is_compact() -> None:
    assert grid_size(1) == (1, 1)
    assert grid_size(4) == (2, 2)
    assert grid_size(5) == (3, 2)
    with pytest.raises(MosaicError):
        grid_size(0)


def test_filter_places_every_input() -> None:
    single, single_resolution = mosaic_filter(1)
    assert single_resolution == "640x360"
    assert single.endswith("[v0]null[outv]")
    graph, resolution = mosaic_filter(3)
    assert resolution == "1280x720"
    assert "[0:v]scale=640:360" in graph
    assert "[v0][v1][v2]xstack=inputs=3:layout=0_0|640_0|0_360" in graph
