# This file is necessary to make this directory a package.

#: Name of the attribute the native decoder sets on constructed ASN.1
#: objects it produces, holding a frozenset of the component names that
#: were present in the source mapping. The native encoder honours this
#: presence information by omitting components that were absent from the
#: source instead of substituting schema defaults for them.
PRESENCE_ATTRIBUTE = '_presentFields'
