package com.example.shop;

import javax.jdo.annotations.VersionStrategy;

/** equals() on constants, array elements and class literals, for where types. */
public class Equals {
    boolean all(String[] names, String[][] grid, Object o) {
        boolean a = names[0].equals("x");
        boolean b = VersionStrategy.NONE.equals(o);
        boolean c = Boolean.TRUE.equals(o);
        boolean d = String.class.equals(o.getClass());
        boolean e = java.io.File.separator.equals("/");
        boolean f = grid[0][1].equals("y");
        return a && b && c && d && e && f;
    }
}
