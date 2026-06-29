"""Parser regression tests using small inline HTML fixtures.

Run:  python -m pytest tests/test_parse.py   (or)   python tests/test_parse.py

These fixtures are self-contained (no network, no gitignored samples) so they
run anywhere. The th/td case guards the fix for ~300-400 products (ASUS
motherboards, PSUs, GPUs, RAM) whose specs live in a
`<th>key</th><td>value</td>` table inside #tab-description and were previously
dropped, yielding 0 specs.
"""

from src.parse import _mine_description_tables
from selectolax.parser import HTMLParser


def _mine(html: str) -> dict:
    return _mine_description_tables(HTMLParser(html))


def test_th_td_rows_are_mined():
    """`<th>key</th><td>value</td>` — the ASUS motherboard/PSU shape."""
    html = """
    <div id="tab-description">
      <table><tbody>
        <tr><th>Brand</th><td>ASUS</td></tr>
        <tr><th>Model</th><td>TUF GAMING B550-PLUS</td></tr>
        <tr><th>CPU Socket Type&nbsp;</th><td>AM4</td></tr>
      </tbody></table>
    </div>"""
    out = _mine(html)
    assert out["Brand"] == "ASUS"
    assert out["Model"] == "TUF GAMING B550-PLUS"
    # entity / NBSP in the key is decoded and stripped
    assert out["CPU Socket Type"] == "AM4"
    assert "CPU Socket Type&nbsp;" not in out


def test_plain_two_td_rows_still_work():
    """The original KOORUI shape must be unaffected."""
    html = """
    <div id="tab-description">
      <table><tbody>
        <tr><td>Specification</td><td>Value</td></tr>
        <tr><td>Brand</td><td>KOORUI</td></tr>
        <tr><td>Model</td><td>G2511X</td></tr>
      </tbody></table>
    </div>"""
    out = _mine(html)
    assert out["Brand"] == "KOORUI"
    assert out["Model"] == "G2511X"
    # the embedded "Specification | Value" header row is skipped
    assert "Specification" not in out


def test_empty_value_rows_skipped():
    """Section-marker rows with an empty value cell are not specs."""
    html = """
    <div id="tab-description">
      <table><tbody>
        <tr><th>DESIGN</th><td></td></tr>
        <tr><th>Weight</th><td>650 g</td></tr>
      </tbody></table>
    </div>"""
    out = _mine(html)
    assert out == {"Weight": "650 g"}


if __name__ == "__main__":
    test_th_td_rows_are_mined()
    test_plain_two_td_rows_still_work()
    test_empty_value_rows_skipped()
    print("all parse tests passed")
