package com.example.shop.util;

import com.example.shop.util.sub.Helper;

/** Moved with Writer; uses a subpackage's type, and a package-private helper. */
public class Reader {
    public String read() {
        return Helper.trim(Writer.header()) + Format.CSV;
    }
}
