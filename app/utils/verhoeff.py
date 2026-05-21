"""
Verhoeff checksum algorithm for Aadhaar validation.

The Verhoeff algorithm detects all single-digit errors and all adjacent
transposition errors. It uses three tables: multiplication (d), permutation (p),
and inverse (inv).

Reference: https://en.wikipedia.org/wiki/Verhoeff_algorithm
"""

# Multiplication table (Dihedral group D5)
_d = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]

# Permutation table
_p = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]

# Inverse table
_inv = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def validate_verhoeff(number: str) -> bool:
    """
    Validate a number string using the Verhoeff checksum algorithm.
    Returns True if the checksum is valid (remainder is 0).
    
    Args:
        number: A string of digits to validate (e.g. "123456789012")
    
    Returns:
        True if the Verhoeff checksum is valid, False otherwise.
    """
    if not number or not number.isdigit():
        return False

    c = 0
    digits = [int(ch) for ch in reversed(number)]
    for i, digit in enumerate(digits):
        c = _d[c][_p[i % 8][digit]]
    return c == 0


def is_valid_aadhaar(aadhaar: str) -> bool:
    """
    Validate an Aadhaar number:
    1. Must be exactly 12 digits
    2. Must not start with 0 or 1
    3. Must pass Verhoeff checksum
    
    Args:
        aadhaar: Raw Aadhaar number string (digits only, no spaces/dashes)
    
    Returns:
        True if valid Aadhaar format, False otherwise.
    """
    if not aadhaar or not aadhaar.isdigit():
        return False
    if len(aadhaar) != 12:
        return False
    if aadhaar[0] in ('0', '1'):
        return False
    return validate_verhoeff(aadhaar)
