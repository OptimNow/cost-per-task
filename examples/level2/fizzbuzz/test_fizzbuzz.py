from fizzbuzz import fizzbuzz


def test_first_fifteen():
    assert fizzbuzz(15) == [
        "1", "2", "Fizz", "4", "Buzz", "Fizz", "7", "8", "Fizz", "Buzz",
        "11", "Fizz", "13", "14", "FizzBuzz",
    ]


def test_zero_gives_empty_list():
    assert fizzbuzz(0) == []


def test_returns_strings():
    assert all(isinstance(item, str) for item in fizzbuzz(30))
