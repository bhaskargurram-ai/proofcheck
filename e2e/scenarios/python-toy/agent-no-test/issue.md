`chunk` silently drops the last, shorter chunk. For example `chunk([1, 2, 3], 2)` returns
`[[1, 2]]`, but the docstring promises `[[1, 2], [3]]`.
