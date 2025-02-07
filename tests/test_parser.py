import unittest
from unittest import mock

import sqlglot.expressions as exp
from sqlglot import ErrorLevel, Parser, ParseError, parse, parse_one


class TestParser(unittest.TestCase):
    def test_column(self):
        columns = parse_one("select a, ARRAY[1] b, case when 1 then 1 end").find_all(
            exp.Column
        )
        assert len(list(columns)) == 1

    def test_identify(self):
        expression = parse_one(
            """
            SELECT a, "b", c AS c, d AS "D", e AS "y|z'"
            FROM y."z"
        """
        )

        assert expression.args["expressions"][0].args["this"].args["this"] == "a"
        assert expression.args["expressions"][1].args["this"].args["this"] == "b"
        assert expression.args["expressions"][2].args["alias"].args["this"] == "c"
        assert expression.args["expressions"][3].args["alias"].args["this"] == "D"
        assert expression.args["expressions"][4].args["alias"].args["this"] == "y|z'"
        table = expression.args["from"].args["expressions"][0]
        assert table.args["this"].args["this"] == "z"
        assert table.args["db"].args["this"] == "y"

    def test_multi(self):
        expressions = parse(
            """
            SELECT * FROM a; SELECT * FROM b;
        """
        )

        assert len(expressions) == 2
        assert (
            expressions[0].args["from"].args["expressions"][0].args["this"].args["this"]
            == "a"
        )
        assert (
            expressions[1].args["from"].args["expressions"][0].args["this"].args["this"]
            == "b"
        )

    @mock.patch("sqlglot.parser.logging")
    def test_expression(self, logging):
        ignore = Parser(error_level=ErrorLevel.IGNORE)
        self.assertIsInstance(ignore.expression(exp.Hint, this=""), exp.Hint)
        self.assertIsInstance(ignore.expression(exp.Hint, y=""), exp.Hint)
        self.assertIsInstance(ignore.expression(exp.Hint), exp.Hint)

        default = Parser()
        self.assertIsInstance(default.expression(exp.Hint, this=""), exp.Hint)
        with self.assertRaises(ParseError):
            default.expression(exp.Hint, y="")
        with self.assertRaises(ParseError):
            default.expression(exp.Hint)

        warn = Parser(error_level=ErrorLevel.WARN)
        warn.expression(exp.Hint, y="")

        assert (
            "Unexpected keyword: 'y' for <class 'sqlglot.expressions.Hint'>. Line 1, Col: 1."
            in str(logging.error.call_args_list[0][0][0])
        )
        warn.expression(exp.Hint)
        assert (
            "Required keyword: 'this' missing for <class 'sqlglot.expressions.Hint'>. Line 1, Col: 1."
            in str(logging.error.call_args_list[1][0][0])
        )

    def test_function_arguments_validation(self):
        with self.assertRaises(ParseError):
            parse_one("IF(a > 0, a, b, c)")

        with self.assertRaises(ParseError):
            parse_one("IF(a > 0)")
