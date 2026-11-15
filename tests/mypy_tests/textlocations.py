#!/usr/bin/env python

def main() -> None:
    from xml.etree.ElementTree import XML
    from elementpath import XPath1Parser
    from elementpath.extras.textlocations import TextMatch, locate_text, \
        iter_locate_text, text_node_source, TEXT, TAIL

    root = XML('<root>head<a>same</a>same<b>same</b>tail</root>')

    matches: list[TextMatch] = locate_text(root, '//text()')
    print(len(matches))

    first: TextMatch = matches[0]
    print(first.value)
    print(first.path)
    print(first.is_tail)
    print(first.text_position)
    print(first.fragment_order)
    print(first.node_position)
    print(first.element_position)
    print(first.source in (TEXT, TAIL))
    print(first.owner.name)
    if first.anchor is not None:
        print(first.anchor.name)

    for match in iter_locate_text(root, '/root/text()', parser=XPath1Parser):
        print(match.value)

    source, owner, anchor = text_node_source(matches[-1].node)
    print(source, owner, anchor)


if __name__ == '__main__':
    main()
