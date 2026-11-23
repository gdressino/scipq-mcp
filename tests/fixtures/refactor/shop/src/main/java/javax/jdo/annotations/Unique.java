package javax.jdo.annotations;

public @interface Unique {
    String name() default "";

    String[] members() default {};
}
