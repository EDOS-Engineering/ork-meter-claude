"""Bounded stdin-only tail recognition for the experimental native collector."""
import re


def bounded_tail(words):
    """Accept literal tail counts only; no files, following or shell expansion.

    ``words`` are scanner (cooked-word, literal) pairs including executable.
    Executable identity is checked by the caller, using its normal admission.
    """
    if not words or not all(isinstance(word,str) and static for word,static in words):
        return False
    args=[word for word,_ in words[1:]]
    count=None
    if len(args)==1:
        match=re.fullmatch(r'-(?:[nc])?([0-9]{1,7})|--(?:lines|bytes)=([0-9]{1,7})',args[0])
        if match:count=next(value for value in match.groups() if value is not None)
    elif len(args)==2 and args[0] in ('-n','-c','--lines','--bytes'):
        if re.fullmatch(r'[0-9]{1,7}',args[1]):count=args[1]
    return count is not None and int(count)<=1000000
