`word_count` counts the empty strings between repeated spaces as words.

```python
from e2e_toy.textops import word_count

n = word_count("hello  world")
assert n == 2, f"expected 2 words, got {n}"
```
