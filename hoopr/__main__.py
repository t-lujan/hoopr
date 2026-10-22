import sys

if len(sys.argv) > 1 and sys.argv[1] == "--web":
    from .web import main

    main(sys.argv[2:])
else:
    from .cli import main

    main()
